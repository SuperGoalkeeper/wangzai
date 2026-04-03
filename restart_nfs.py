#!/usr/bin/env python3
"""SSH to NAS and restart NFS service"""
import pexpect
import sys

NAS_IP = "192.168.3.217"
NAS_USER = "admin"
NAS_PASS = "331615qq"

def ssh_run(cmds, timeout=30):
    child = pexpect.spawn(f'ssh {NAS_USER}@{NAS_IP}', timeout=timeout)
    child.expect('password:')
    child.sendline(NAS_PASS)
    child.expect(r'[\$#]')

    for cmd in cmds:
        child.sendline(cmd)
        child.expect(r'[\$#]')
        output = child.before.decode().strip()
        print(f">>> {cmd}")
        print(output)
        print()

    child.sendline('exit')
    child.close()

if __name__ == "__main__":
    print("=== 连接 NAS 重启 NFS 服务 ===\n")

    cmds = [
        "uname -a",
        # Check NFS service status
        "systemctl status nfs-server 2>&1 || service nfs-server status 2>&1 || echo 'trying other names...'",
        # Try different NFS service names (QNAP might use different names)
        "ps aux | grep -i nfs | grep -v grep",
        # Restart NFS
        "systemctl restart nfs-server 2>&1 || service nfs-server restart 2>&1 || /etc/init.d/nfs-server restart 2>&1 || echo 'trying QNAP way...'",
        # QNAP specific
        "/etc/init.d/nfsd restart 2>&1 || /etc/init.d/nfs restart 2>&1 || echo 'NFS restart attempted'",
        # Check status after restart
        "ps aux | grep -i nfs | grep -v grep",
    ]

    try:
        ssh_run(cmds, timeout=60)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
