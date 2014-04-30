Ansible Winning
===============

Initial hacking on a WinRM connection plugin for Ansible using PyWinRM.

Danger Ahead!
-------------

Don't use for anything even remotely important at this point.  It does not use
HTTPS, makes lots of assumptions about Ansible runner commands, may not
properly escape all parameters, and may result in something catching on fire.

You've been warned.

Running the Example Playbook
----------------------------

Ok, fine. You want to try it out?

Edit `winners.txt` to specify your Windows hosts and credentials.

Make sure those servers have Powershell Remoting enabled (run `winrm quickconfig`),
listening on HTTP port 5985 and allow remote scripting (run `Set-ExecutionPolicy RemoteSigned`).

Run `./winders.sh` to ping your Windows hosts from Ansible.

If it doesn't work for you, oh well. See the warnings above.
