# Collection of hacks to allow connection to work with unmodified Ansible.
import inspect
import os
import re
from ansible.utils.plugins import module_finder

def patch_module_finder(_conn):
    # Monkeypatch module finder to attempt to load modules with .ps1
    # suffix first when using this connection plugin.
    orig_find_plugin = module_finder.find_plugin
    def find_plugin(name):
        try_ps1_suffix = False
        for frame_tuple in inspect.stack():
            frame_locals = inspect.getargvalues(frame_tuple[0])[3]
            if 'conn' in frame_locals:
                if frame_locals['conn'] is _conn:
                    try_ps1_suffix = True
                break
        module_path = None
        if try_ps1_suffix and not name.lower().endswith('.ps1'):
            module_path = orig_find_plugin('%s.ps1' % name)
        if not module_path:
            module_path = orig_find_plugin(name)
        return module_path
    module_finder.find_plugin = find_plugin

def get_port(conn):
    # Ansible doesn't pass ansible_ssh_port to the connection, so we have to
    # get it ourselves.
    port = conn.port
    if not port:
        host = conn.delegate or conn.host
        host_vars = conn.runner.inventory.get_variables(host, vault_password=conn.runner.vault_pass)
        port = int(host_vars.get('ansible_ssh_port', None) or 5986)
    return port

def fix_slashes(path):
    return path.replace('/', '\\')

def filter_cmd_parts(conn, cmd_parts):
    try:
        # HACK: Catch runner _make_tmp_path() call and replace with PS.
        if cmd_parts[0] == 'mkdir' and cmd_parts[1] == '-p' and cmd_parts[-3] == '&&' and cmd_parts[-2] == 'echo' and cmd_parts[2] == cmd_parts[-1] and '-tmp-' in os.path.basename(cmd_parts[2]):
            basename = os.path.basename(cmd_parts[2])
            script = '''(New-Item -Type Directory -Path $env:temp -Name "%s").FullName;''' % conn._winrm_escape(basename)
            return conn._winrm_get_script_cmd(script)
        # HACK: Catch runner _execute_module() chmod calls and ignore.
        if cmd_parts[0] == 'chmod' and '-tmp-' in cmd_parts[2]:
            return [] # No-op.
        # HACK: Catch runner _remove_tmp_path() call and replace with PS.
        if cmd_parts[0] == 'rm' and cmd_parts[1] == '-rf' and '-tmp-' in cmd_parts[2]:
            path = fix_slashes(cmd_parts[2])
            script = '''Remove-Item "%s" -Force -Recurse;''' % conn._winrm_escape(path)
            return conn._winrm_get_script_cmd(script)
        # HACK: Catch runner _remote_md5() call and replace with PS.
        if cmd_parts[0] == 'rc=0;' and '(/usr/bin/md5sum' in cmd_parts:
            path = cmd_parts[cmd_parts.index('(/usr/bin/md5sum') + 1]
            if path.startswith('\'') and path.endswith('\''):
                path = path[1:-1]
            path = fix_slashes(path)
            script = '''(Get-FileHash -Path "%s" -Algorithm MD5).Hash.ToLower();''' % conn._winrm_escape(path)
            return conn._winrm_get_script_cmd(script)
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
            mod_path = fix_slashes(cmd_parts[1])
            if not mod_path.lower().endswith('.ps1'):
                mod_path = '%s.ps1' % mod_path
            args_path = fix_slashes(cmd_parts[2]).rstrip(';')
            script = '''PowerShell -NoProfile -NonInteractive -ExecutionPolicy Unrestricted -File "%s" "%s";''' % (conn._winrm_escape(mod_path), conn._winrm_escape(args_path))
            if len(cmd_parts) >= 6 and cmd_parts[3] == 'rm' and cmd_parts[4] == '-rf' and '-tmp-' in cmd_parts[5]:
                path = fix_slashes(cmd_parts[5])
                script += ''' Remove-Item "%s" -Force -Recurse;''' % conn._winrm_escape(path)
            return conn._winrm_get_script_cmd(script)
        # HACK: Catch command executed by the script module, remove env vars.
        if any(re.match(r'^[A-Z0-9_]+?=.+?$', x) for x in cmd_parts):
            new_parts = [x for x in cmd_parts if not re.match(r'^[A-Z0-9_]+?=.+?$', x)]
            if len(new_parts) >= 1 and new_parts[0].lower().endswith('.ps1'):
                return ['PowerShell', '-ExecutionPolicy', 'Unrestricted', '-File', fix_slashes(new_parts[0])]
    except IndexError:
        pass
    return cmd_parts
