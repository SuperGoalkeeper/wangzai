#!/usr/bin/env python3
"""SSH to NAS - flush NFS exports and check client connections"""
import pexpect

NAS_IP = "192.168.3.217"
NAS_USER = "admin"
NAS_PASS = "331615qq"

child = pexpect.spawn(f'ssh {NAS_USER}@{NAS_IP}', timeout=15)
child.expect('password:')
child.sendline(NAS_PASS)
child.expect(r'[\$#]')

cmds = [
    # Re-export all shares
    'sudo -S exportfs -ra <<< "331615qq"',
    # Check NFS exports
    'exportfs -v',
    # Check NFS client connections
    'cat /proc/net/rpc/nfsd',
    # Check NFS server status
    'cat /var/lib/nfs/state 2>/dev/null || echo "no state file"',
    # Show connected clients
    'showmount -a 2>/dev/null || cat /proc/fs/nfsd/clients 2>/dev/null || echo "no client info"',
]

for cmd in cmds:
    child.sendline(cmd)
    child.expect(r'[\$#]', timeout=15)
    output = child.before.decode().strip()
    # Filter out command echo
    lines = output.split('\n')
    relevant = [l for l in lines if not l.startswith('admin@') and cmd not in l]
    print('\n'.join(relevant))
    print("---")

child.sendline('exit')
child.close()
