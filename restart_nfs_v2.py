#!/usr/bin/env python3
"""SSH to NAS and restart NFS - handle polkit auth"""
import pexpect
import sys

NAS_IP = "192.168.3.217"
NAS_USER = "admin"
NAS_PASS = "331615qq"

def ssh_run():
    child = pexpect.spawn(f'ssh {NAS_USER}@{NAS_IP}', timeout=15)
    child.expect('password:')
    child.sendline(NAS_PASS)
    child.expect(r'[\$#]')

    # Method 1: Try sudo
    print("=== 尝试 sudo 重启 NFS ===")
    child.sendline(f'echo {NAS_PASS} | sudo -S systemctl restart nfs-server 2>&1')
    idx = child.expect([r'[\$#]', 'password:', 'Sorry'], timeout=20)
    if idx == 1:
        child.sendline(NAS_PASS)
        child.expect(r'[\$#]', timeout=20)
    print(child.before.decode().strip())

    # Check status
    child.sendline('systemctl status nfs-server')
    child.expect(r'[\$#]', timeout=15)
    print(child.before.decode().strip())

    # Also try exportfs to re-export
    child.sendline('sudo -S exportfs -ra 2>&1')
    idx = child.expect([r'[\$#]', 'password:'], timeout=15)
    if idx == 1:
        child.sendline(NAS_PASS)
        child.expect(r'[\$#]', timeout=15)
    print(child.before.decode().strip())

    # Kill stuck nfsd threads and let them restart
    child.sendline('ps aux | grep nfsd | grep -v grep')
    child.expect(r'[\$#]', timeout=10)
    print(child.before.decode().strip())

    child.sendline('exit')
    child.close()

if __name__ == "__main__":
    try:
        ssh_run()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
