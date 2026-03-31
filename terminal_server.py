import asyncio
import paramiko
import websockets
from urllib.parse import urlparse, parse_qs


async def handle_ws(websocket, path):
    # parse query params: host, user, pwd, dom
    qs = parse_qs(urlparse(path).query)
    host = qs.get('host', [None])[0]
    user = qs.get('user', [None])[0]
    pwd = qs.get('pwd', [None])[0]
    dom = qs.get('dom', [None])[0]

    if not all([host, user, pwd, dom]):
        await websocket.send('ERROR: missing host/user/pwd/dom in query')
        await websocket.close()
        return

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=pwd, timeout=5)
    except Exception as e:
        await websocket.send(f'ERROR: SSH connect failed: {e}')
        await websocket.close()
        return

    try:
        chan = ssh.get_transport().open_session()
        chan.get_pty()
        chan.exec_command(f"virsh console --force '{dom}'")

        async def ssh_to_ws():
            try:
                while True:
                    await asyncio.sleep(0.01)
                    if chan.recv_ready():
                        data = chan.recv(4096)
                        try:
                            text = data.decode(errors='ignore')
                        except Exception:
                            text = str(data)
                        await websocket.send(text)
                    if chan.recv_stderr_ready():
                        data = chan.recv_stderr(4096)
                        try:
                            text = data.decode(errors='ignore')
                        except Exception:
                            text = str(data)
                        await websocket.send(text)
                    if chan.exit_status_ready():
                        break
            except Exception:
                pass

        async def ws_to_ssh():
            try:
                async for msg in websocket:
                    if isinstance(msg, str):
                        chan.send(msg)
                    else:
                        # binary
                        chan.send(msg.decode(errors='ignore'))
            except Exception:
                pass

        await asyncio.gather(ssh_to_ws(), ws_to_ssh())

    finally:
        try:
            chan.close()
        except Exception:
            pass
        try:
            ssh.close()
        except Exception:
            pass


def run_server(host='localhost', port=8765):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    start_server = websockets.serve(handle_ws, host, port)
    loop.run_until_complete(start_server)
    loop.run_forever()
