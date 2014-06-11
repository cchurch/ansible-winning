# PowerShell script to bootstrap WinRM over HTTP.  You'll need to run these
# commands on the remote system to allow Ansible connect over HTTP.

# Source: http://technet.microsoft.com/en-us/library/hh847850.aspx
Enable-PSRemoting -Force
Set-NetFirewallRule -Name "WINRM-HTTP-In-TCP-PUBLIC" -RemoteAddress Any
