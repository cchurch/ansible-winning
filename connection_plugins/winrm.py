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
from winrm.protocol import Protocol

WINRM_CONNECTION_CACHE = {}

class Connection(object):
    '''WinRM connections over HTTP/HTTPS.'''

    def __init__(self,  runner, host, port, user, password, *args, **kwargs):
        self.runner = runner
        self.host = host
        self.port = port or 5985
        self.user = user
        self.password = password
        self.has_pipelining = False
        self.protocol = None
        self.shell_id = None

    def _winrm_connect(self):
        vvv("ESTABLISH WINRM CONNECTION FOR USER: %s on PORT %s TO %s" % (self.user, self.port, self.host), host=self.host)
        netloc = '%s:%d' % (self.host, self.port)
        url = urlparse.urlunsplit(('http', netloc, '/wsman', '', ''))
        return Protocol(url, username=self.user, password=self.password)

    def _winrm_escape(self, value, include_vars=False):
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

    def _winrm_filter(self, cmd_parts):
        try:
            # HACK: Catch runner _make_tmp_path() call and replace with PS.
            if cmd_parts[0] == 'mkdir' and cmd_parts[1] == '-p' and cmd_parts[-3] == '&&' and cmd_parts[-2] == 'echo' and cmd_parts[2] == cmd_parts[-1]:
                basename = os.path.basename(cmd_parts[2])
                script = '''(New-Item -Type Directory -Path $env:temp -Name "%s").FullName;''' % self._winrm_escape(basename)
                return self._winrm_get_script_cmd(script)
            # HACK: Catch runner _execute_module() chmod calls and ignore.
            if cmd_parts[0] == 'chmod' and len(cmd_parts) == 3:
                return [] # No-op.
            # HACK: Catch runner _remove_tmp_path() call and replace with PS.
            if cmd_parts[0] == 'rm' and cmd_parts[1] == '-rf' and cmd_parts[2]:
                script = '''Remove-Item "%s" -Force -Recurse;''' % self._winrm_escape(cmd_parts[2])
                return self._winrm_get_script_cmd(script)
            # HACK: Catch runner _remote_md5() call and replace with PS.
            if cmd_parts[0] == 'rc=0;' and '(/usr/bin/md5sum' in cmd_parts:
                path = cmd_parts[cmd_parts.index('(/usr/bin/md5sum') + 1]
                if path.startswith('\'') and path.endswith('\''):
                    path = path[1:-1]
                path = path.replace('/', '\\')
                script = '''(Get-FileHash -Path "%s" -Algorithm MD5).Hash.ToLower();''' % self._winrm_escape(path)
                return self._winrm_get_script_cmd(script)
            # HACK: Catch 
            if 'powershell' in cmd_parts:
                env_vars = {}
                for n, cmd_part in enumerate(cmd_parts):
                    if cmd_part == 'powershell':
                        cmd_parts = cmd_parts[n:]
                        break
                    elif '=' in cmd_part:
                        var, val = cmd_part.split('=', 1)
                        env_vars[var] = val
                new_cmd_parts = [
                    'powershell',
                    '-NoProfile', '-NonInteractive',
                    '-File',
                    '%s.ps1' % cmd_parts[1].replace('/', '\\'),
                    cmd_parts[2].replace('/', '\\').rstrip(';'),
                ]
                if len(cmd_parts) >= 6 and cmd_parts[3] == 'rm' and False:
                    new_cmd_parts.extend([
                        'Remove-Item',
                        self._winrm_escape(cmd_parts[5].replace('/', '\\')),
                        '-Force',
                        '-Recurse',
                    ])
                return new_cmd_parts
        except IndexError:
            pass
        return cmd_parts

    def _winrm_get_script_cmd(self, script):
        vvvv('WINRM SCRIPT %s' % script, host=self.host)
        encoded_script = base64.b64encode(script.encode('utf-16-le'))
        return ['PowerShell -NoProfile -NonInteractive -EncodedCommand %s' % encoded_script]

    def _winrm_exec(self, command, args):
        vvvv("WINRM EXEC %r %r" % (command, args), host=self.host)
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
        cache_key = "%s__%d__%s__" % (self.host, self.port, self.user)
        if cache_key in WINRM_CONNECTION_CACHE:
            self.protocol = WINRM_CONNECTION_CACHE[cache_key]
        else:
            self.protocol = WINRM_CONNECTION_CACHE[cache_key] = self._winrm_connect()
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
                        if out_data.splitlines()[0].startswith('#!powershell'):
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
