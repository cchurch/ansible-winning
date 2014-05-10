# (c) 2014, Chris Church <chris@ninemoreminutes.com>
#
# This file is (not yet) part of Ansible.
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import absolute_import

import base64
import os
import re
import shlex
import shutil
import traceback
import urlparse
from ansible import errors
from ansible import utils
from ansible.callbacks import vvv, vvvv

from winrm import Response
from winrm.exceptions import WinRMTransportError
from winrm.protocol import Protocol


class Connection(object):
    '''WinRM connections over HTTP/HTTPS.'''

    def __init__(self,  runner, host, port, user, password, *args, **kwargs):
        self.runner = runner
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.has_pipelining = False
        self.protocol = None
        self.shell_id = None
        self.delegate = None

        # Monkeypatch module finder to attempt to load modules with .ps1
        # suffix first when using this connection plugin.
        import inspect
        from ansible.utils.plugins import module_finder
        orig_find_plugin = module_finder.find_plugin
        def find_plugin(name):
            try_ps1_suffix = False
            for frame_tuple in inspect.stack():
                frame_locals = inspect.getargvalues(frame_tuple[0])[3]
                if 'conn' in frame_locals:
                    if frame_locals['conn'] is self:
                        try_ps1_suffix = True
                    break
            module_path = None
            if try_ps1_suffix and not name.lower().endswith('.ps1'):
                module_path = orig_find_plugin('%s.ps1' % name)
            if not module_path:
                module_path = orig_find_plugin(name)
            return module_path
        module_finder.find_plugin = find_plugin

    def _winrm_connect(self):
        '''
        Establish a WinRM connection over HTTP/HTTPS.
        '''
        # Ansible doesn't pass ansible_ssh_port to this connection, so we have
        # to get it ourselves.
        port = self.port
        if not port:
            host = self.delegate or self.host
            host_vars = self.runner.inventory.get_variables(host, vault_password=self.runner.vault_pass)
            port = int(host_vars.get('ansible_ssh_port', None) or 5986)
        vvv("ESTABLISH WINRM CONNECTION FOR USER: %s on PORT %s TO %s" % \
            (self.user, port, self.host), host=self.host)
        netloc = '%s:%d' % (self.host, port)
        transport_schemes = [('plaintext', 'https'), ('plaintext', 'http')] # FIXME: ssl/kerberos
        if port == 5985:
            transport_schemes = reversed(transport_schemes)
        exc = None
        for transport, scheme in transport_schemes:
            endpoint = urlparse.urlunsplit((scheme, netloc, '/wsman', '', ''))
            vvvv('WINRM CONNECT: transport=%s endpoint=%s' % (transport, endpoint),
                 host=self.host)
            protocol = Protocol(endpoint, transport=transport,
                                username=self.user, password=self.password)
            try:
                protocol.send_message('')
                return protocol
            except WinRMTransportError, exc:
                err_msg = str(exc.args[0])
                if re.search(r'Operation\s+?timed\s+?out', err_msg, re.I):
                    raise
                m = re.search(r'Code\s+?(\d{3})', err_msg)
                if m:
                    code = int(m.groups()[0])
                    if code == 411:
                        return protocol
                vvvv('WINRM CONNECTION ERROR: %s' % err_msg, host=self.host)
                continue
        if exc:
            raise exc

    def _winrm_escape(self, value, include_vars=False):
        '''
        Return value escaped for use in PowerShell command.
        '''
        # http://www.techotopia.com/index.php/Windows_PowerShell_1.0_String_Quoting_and_Escape_Sequences
        # http://stackoverflow.com/questions/764360/a-list-of-string-replacements-in-python
        subs = [('\n', '`n'), ('\r', '`r'), ('\t', '`t'), ('\a', '`a'),
                ('\b', '`b'), ('\f', '`f'), ('\v', '`v'), ('"', '`"'),
                ('\'', '`\''), ('`', '``'), ('\x00', '`0')]
        if include_vars:
            subs.append(('$', '`$'))
        pattern = '|'.join('(%s)' % re.escape(p) for p, s in subs)
        substs = [s for p, s in subs]
        replace = lambda m: substs[m.lastindex - 1]
        return re.sub(pattern, replace, value)

    def _winrm_get_script_cmd(self, script):
        '''
        Convert a PowerShell script to a single base64-encoded command.
        '''
        vvvv('WINRM SCRIPT: %s' % script, host=self.host)
        encoded_script = base64.b64encode(script.encode('utf-16-le'))
        return ['PowerShell', '-NoProfile', '-NonInteractive',
                '-EncodedCommand', encoded_script]

    def _winrm_filter(self, cmd_parts):
        try:
            # HACK: Catch runner _make_tmp_path() call and replace with PS.
            if cmd_parts[0] == 'mkdir' and cmd_parts[1] == '-p' and cmd_parts[-3] == '&&' and cmd_parts[-2] == 'echo' and cmd_parts[2] == cmd_parts[-1] and '-tmp-' in os.path.basename(cmd_parts[2]):
                basename = os.path.basename(cmd_parts[2])
                script = '''(New-Item -Type Directory -Path $env:temp -Name "%s").FullName;''' % self._winrm_escape(basename)
                return self._winrm_get_script_cmd(script)
            # HACK: Catch runner _execute_module() chmod calls and ignore.
            if cmd_parts[0] == 'chmod' and '-tmp-' in cmd_parts[2]:
                return [] # No-op.
            # HACK: Catch runner _remove_tmp_path() call and replace with PS.
            if cmd_parts[0] == 'rm' and cmd_parts[1] == '-rf' and '-tmp-' in cmd_parts[2]:
                path = cmd_parts[2].replace('/', '\\')
                script = '''Remove-Item "%s" -Force -Recurse;''' % self._winrm_escape(path)
                return self._winrm_get_script_cmd(script)
            # HACK: Catch runner _remote_md5() call and replace with PS.
            if cmd_parts[0] == 'rc=0;' and '(/usr/bin/md5sum' in cmd_parts:
                path = cmd_parts[cmd_parts.index('(/usr/bin/md5sum') + 1]
                if path.startswith('\'') and path.endswith('\''):
                    path = path[1:-1]
                path = path.replace('/', '\\')
                script = '''(Get-FileHash -Path "%s" -Algorithm MD5).Hash.ToLower();''' % self._winrm_escape(path)
                return self._winrm_get_script_cmd(script)
            # HACK: Catch the call to run the PS module.
            if any(x.lower().startswith('powershell') for x in cmd_parts):
                env_vars = {}
                for n, cmd_part in enumerate(cmd_parts):
                    if cmd_part.lower().startswith('powershell'):
                        cmd_parts = cmd_parts[n:]
                        break
                    elif '=' in cmd_part:
                        var, val = cmd_part.split('=', 1)
                        env_vars[var] = val
                mod_path = cmd_parts[1].replace('/', '\\')
                if not mod_path.lower().endswith('.ps1'):
                    mod_path = '%s.ps1' % mod_path
                args_path = cmd_parts[2].replace('/', '\\').rstrip(';')
                script = '''PowerShell -NoProfile -NonInteractive -File "%s" "%s";''' % (self._winrm_escape(mod_path), self._winrm_escape(args_path))
                if len(cmd_parts) >= 6 and cmd_parts[3] == 'rm' and cmd_parts[4] == '-rf' and '-tmp-' in cmd_parts[5]:
                    path = cmd_parts[5].replace('/', '\\')
                    script += ''' Remove-Item "%s" -Force -Recurse;''' % self._winrm_escape(path)
                return self._winrm_get_script_cmd(script)
        except IndexError:
            pass
        return cmd_parts

    def _winrm_exec(self, command, args):
        vvvv("WINRM EXEC %r %r" % (command, args), host=self.host)
        if not self.protocol:
            self.protocol = self._winrm_connect()
        if not self.shell_id:
            self.shell_id = self.protocol.open_shell()
        command_id = None
        try:
            command_id = self.protocol.run_command(self.shell_id, command, args)
            response = Response(self.protocol.get_command_output(self.shell_id, command_id))
            vvvv('WINRM RESULT %r' % response, host=self.host)
            vvvv('WINRM STDERR %s' % response.std_err, host=self.host)
            return response
        finally:
            if command_id:
                self.protocol.cleanup_command(self.shell_id, command_id)

    def connect(self):
        # No-op. Connect lazily on first command, to allow for runner to set
        # self.delegate, needed if actual host vs. host name are different.
        return self

    def exec_command(self, cmd, tmp_path, sudo_user=None, sudoable=False, executable='/bin/sh', in_data=None, su=None, su_user=None):
        cmd = cmd.encode('utf-8')
        vvv("EXEC %s" % cmd, host=self.host)
        cmd_parts = shlex.split(cmd, posix=False)
        vvvv("WINRM PARTS %r" % cmd_parts, host=self.host)
        cmd_parts = self._winrm_filter(cmd_parts)
        if not cmd_parts:
            vvv('WINRM NOOP')
            return (0, '', '', '')
        try:
            result = self._winrm_exec(cmd_parts[0], cmd_parts[1:])
        except Exception, e:
            traceback.print_exc()
            raise errors.AnsibleError("failed to exec cmd %s" % cmd)
        return (result.status_code, '', result.std_out.encode('utf-8'), result.std_err.encode('utf-8'))

    def put_file(self, in_path, out_path):
        out_path = out_path.replace('/', '\\')
        vvv("PUT %s TO %s" % (in_path, out_path), host=self.host)
        if not os.path.exists(in_path):
            raise errors.AnsibleFileNotFound("file or module does not exist: %s" % in_path)
        chunk_size = 1024 # FIXME: Find max size or optimize.
        with open(in_path) as in_file:
            for offset in xrange(0, os.path.getsize(in_path), chunk_size):
                try:
                    out_data = in_file.read(chunk_size)
                    if offset == 0:
                        if out_data.lower().startswith('#!powershell') and not out_path.lower().endswith('.ps1'):
                            out_path = out_path + '.ps1'
                        script = '''New-Item -Path "%s" -Type File -Value "%s";''' % (self._winrm_escape(out_path), self._winrm_escape(out_data, True))
                    else:
                        script = '''Add-Content -Path "%s" -Value "%s";''' % (self._winrm_escape(out_path), self._winrm_escape(out_data, True))
                    cmd_parts = self._winrm_get_script_cmd(script)
                    self._winrm_exec(cmd_parts[0], cmd_parts[1:])
                except Exception:#IOError:
                    traceback.print_exc()
                    raise errors.AnsibleError("failed to transfer file to %s" % out_path)

    def fetch_file(self, in_path, out_path):
        in_path = in_path.replace('/', '\\')
        out_path = out_path.replace('\\', '/')
        vvv("FETCH %s TO %s" % (in_path, out_path), host=self.host)
        chunk_size = 1024 # FIXME: How to get file in chunks?
        if not os.path.exists(os.path.dirname(out_path)):
            os.makedirs(os.path.dirname(out_path))
        with open(out_path, 'wb') as out_file:
            while True:
                try:
                    # FIXME: Which encoding for binary files?
                    script = '''Get-Content -Path "%s" -Encoding UTF8 -ReadCount 0;''' % self._winrm_escape(in_path)
                    cmd_parts = self._winrm_get_script_cmd(script)
                    result = self._winrm_exec(cmd_parts[0], cmd_parts[1:])
                    out_file.write(result.std_out)
                    break
                except Exception:#IOError:
                    traceback.print_exc()
                    raise errors.AnsibleError("failed to transfer file to %s" % out_path)

    def close(self):
        if self.protocol and self.shell_id:
            self.protocol.close_shell(self.shell_id)
            self.shell_id = None
