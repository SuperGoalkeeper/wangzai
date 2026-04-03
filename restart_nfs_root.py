#!/usr/bin/env python3
"""SSH to NAS as root and restart NFS service"""
import pexpect
import sys

NAS_IP = "192.168.3.217"

def ssh_run():
    # Try root login with same password
    for user, pwd in [("root", "331615qq"), ("admin", "331615qq")]:
        print(f"Trying {user}@{NAS_IP}...")
        child = pexpect.spawn(f'ssh {user}@{NAS_IP}', timeout=15)
        i = child.expect(['password:', r'[\$#]', 'Permission denied', pexpect.TIMEOUT], timeout=10)
        if i == 0:
            child.sendline(pwd)
            j = child.expect([r'[\$#]', 'Permission denied', pexpect.TIMEOUT], timeout=10)
            if j == 0:
                print(f"Logged in as {user}")
                # Restart NFS
                child.sendline('systemctl restart nfs-server')
                child.expect(r'[\$#]', timeout=30)
                print(child.before.decode().strip())

                child.sendline('systemctl status nfs-server')
                child.expect(r'[\$#]', timeout=15)
                print(child.before.decode().strip())

                child.sendline('exportfs -ra')
                child.expect(r'[\$#]', timeout=15)
                print(child.before.decode().strip())

                child.sendline('exit')
                child.close()
                return True
            else:
                print(f"  Password rejected for {user}")
        elif i == 1:
            print(f"  Already logged in as {user}")
            child.sendline('systemctl restart nfs-server')
            child.expect(r'[\$#]', timeout=30)
            print(child.before.decode().strip())
            child.sendline('exit')
            child.close()
            return True
        else:
            print(f"  Login failed for {user}")
        child.close()

    return False

if __name__ == "__main__":
    if ssh_run():
        print("\n✅ NFS service restarted!")
    else:
        print("\n❌ Failed to restart NFS service - need root access")
        sys.exit(1)
