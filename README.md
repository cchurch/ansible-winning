Ansible Winning
===============

Initial hacking on a WinRM connection plugin for Ansible using PyWinRM.

**UPDATE 2014/06/19:** This code and many updates to it have been merged into
Ansible devel via [Windows Remote Support #7861](https://github.com/ansible/ansible/pull/7861/).

This project is not likely to be maintained from now on.  Submit your bug
reports and pull requests to [Ansible](https://github.com/ansible/ansible/).

Disclaimer
----------

Don't use for anything even remotely important at this point.  It uses HTTP by
default (though it can use HTTPS), makes lots of assumptions about Ansible
runner commands, may not properly escape all parameters or fix all
forward/back slashes, and may result in something catching on fire.

You've been warned.

Initial Setup
-------------

1. Clone this repo.
1. Install Python packages (use a virtual if that's your thing): `pip install -r requirements.txt`
1. Configure your Windows system for PowerShell remoting over HTTP: see `files\bootstrap.ps1`

Running the Example Playbook
----------------------------

Edit `winners.txt` to specify your Windows hosts and credentials.

Make sure those servers have Powershell Remoting enabled (run `winrm quickconfig`),
listening on HTTP port 5985.

Run `./winders.sh` to ping your Windows hosts from Ansible.
