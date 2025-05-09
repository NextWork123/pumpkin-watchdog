import asyncio
import json
import os
import signal
import sys
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from typing import IO, Any, List, Optional, Union
from functools import lru_cache
from aiohttp import web


@dataclass(frozen=True)
class SubprocessError(Exception):
    stdout: Optional[bytes]
    stderr: Optional[bytes]

    def __str__(self) -> str:
        return self.stderr.decode() if self.stderr else self.stdout.decode() if self.stdout else "NO OUTPUT"

    def __repr__(self) -> str:
        if not self.stderr and not self.stdout:
            return "NO OUTPUT"
        return "\n\n".join(filter(None, [
            f"STDOUT: {self.stdout.decode()}" if self.stdout else None,
            f"STDERR: {self.stderr.decode()}" if self.stderr else None
        ]))


async def run_command(cmd: str, env: Optional[dict] = None) -> None:
    proc = await asyncio.subprocess.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env
    )
    exit_code = await proc.wait()
    if exit_code != 0:
        raise SubprocessError(
            await proc.stdout.read() if proc.stdout else None,
            await proc.stderr.read() if proc.stderr else None
        )


@lru_cache(maxsize=128)
def get_log_file_path(log_dir: str, commit: str, counter: int, is_stderr: bool = False) -> str:
    return os.path.join(log_dir, commit, f"{'stderr' if is_stderr else 'stdout'}_{counter}.txt")


async def update_rust() -> None:
    print("updating rust")
    await run_command("rustup update")


async def update_git_repo(repo_dir: str) -> None:
    print(f"updating repo: {repo_dir}")
    os.chdir(repo_dir)
    await run_command("git reset --hard")
    await run_command("git pull origin master")


async def clean_current_directory() -> None:
    await run_command("cargo clean")


async def clean_binary(repo_dir: str, plugins_dir: str) -> None:
    print("cleaning build dir")
    os.chdir(repo_dir)
    await clean_current_directory()

    print("cleaning plugins dir")
    tasks = []
    for path in os.scandir(plugins_dir):
        os.chdir(path.path)
        tasks.append(clean_current_directory())
    await asyncio.gather(*tasks)


async def build_plugins(repo_dir: str, plugins_dir: str) -> None:
    plugin_final_dir = os.path.join(repo_dir, "./plugins")
    os.makedirs(plugin_final_dir, exist_ok=True)
    
    build_tasks = []
    for plugin_path in os.scandir(plugins_dir):
        if plugin_path.is_dir():
            build_tasks.append(build_repo(plugin_path.path, False))
    
    if build_tasks:
        await asyncio.gather(*build_tasks)
        
        for plugin_path in os.scandir(plugins_dir):
            if not plugin_path.is_dir():
                continue
                
            plugin_output_dir = os.path.join(plugin_path.path, "./target/release/")
            if not os.path.exists(plugin_output_dir):
                print(f"[WARN] Build output directory not found for {plugin_path.name}")
                continue
            
            for path in os.scandir(plugin_output_dir):
                if path.name.endswith('.so') or path.name.endswith('.dll'):
                    final_path = os.path.join(plugin_final_dir, path.name)
                    print(f"moving {path.path} to {final_path}")
                    try:
                        if os.path.exists(final_path):
                            os.remove(final_path)
                        os.rename(path.path, final_path)
                        break
                    except OSError as e:
                        print(f"[ERROR] Failed to move plugin {path.name}: {e}")
            else:
                print(f"[WARN] Couldn't find a plugin for {plugin_path.name}")
    else:
        print("[INFO] No plugins found to build")


async def build_repo(repo_dir: str, target_native: bool) -> None:
    os.chdir(repo_dir)
    env = os.environ.copy()
    if target_native:
        env["RUSTFLAGS"] = "-C target-cpu=native"
    
    await run_command(
        "cargo build --release",
        env=env
    )


async def get_repo_description(repo_dir: str) -> List[str]:
    os.chdir(repo_dir)
    proc = await asyncio.subprocess.create_subprocess_shell(
        'git log -n 1 --pretty=format:"%H\n%s"',
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    exit_code = await proc.wait()
    if exit_code == 0 and proc.stdout:
        result = await proc.stdout.read()
        return result.decode().splitlines()

    raise SubprocessError(
        await proc.stdout.read() if proc.stdout else None,
        await proc.stderr.read() if proc.stderr else None
    )


async def start_binary(
    binary_path: str,
    stdout_log: IO[Any],
    stderr_log: IO[Any],
) -> asyncio.subprocess.Process:
    env = {
        **os.environ,
        "RUST_BACKTRACE": "1",
        "RUST_LOG": "debug"
    }
    return await asyncio.subprocess.create_subprocess_exec(
        binary_path,
        stdout=stdout_log,
        stderr=stderr_log,
        env=env,
    )


async def wait_for_process_or_signal(
    proc: asyncio.subprocess.Process,
    update_queue: asyncio.Queue[str],
) -> Optional[str]:
    proc_task = asyncio.create_task(proc.wait())
    kill_task = asyncio.create_task(update_queue.get())
    
    try:
        done, pending = await asyncio.wait(
            [proc_task, kill_task],
            return_when=asyncio.FIRST_COMPLETED
        )
        
        kill_task.cancel()
        
        if proc_task in done:
            return_code = proc_task.result()
            print(f"binary returned code {return_code}")
            return None
            
        proc.send_signal(signal.SIGINT)
        try:
            return_code = await asyncio.wait_for(proc_task, 120)
        except asyncio.TimeoutError:
            print("failed to terminate task, killing")
            proc.kill()
            try:
                return_code = await proc_task
            except asyncio.CancelledError:
                print("server processes cancelled")
                return_code = None

        print(f"binary was terminated with code {return_code}")
        
        new_commit = kill_task.result()
        while not update_queue.empty():
            new_commit = await update_queue.get()
            
        return new_commit
        
    finally:
        for task in [proc_task, kill_task]:
            if not task.done():
                task.cancel()


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <meta name="color-scheme" content="light dark" />
    <title>Nightly PumpkinMC</title>
    <meta name="description" content="A nightly pumpkin minecraft server." />
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2.0.6/css/pico.min.css" />
  </head>
  <body>
    <div style="margin:15px;">
        <h1>A Nightly PumpkinMC Server</h1>
        {error}
        <p>This server is running <a href=https://github.com/Snowiiii/Pumpkin>Pumpkin MC</a> -- a Rust-written vanilla minecraft server -- straight from the master branch, updated when commits are made to the master branch (currently running <a href="https://github.com/Snowiiii/Pumpkin/commit/{commit}">({short_commit}) {name}</a>).</p>
        <p>You can join and test with your Minecraft client at <b>pumpkin.kralverde.dev</b> on port <b>25565</b>. The goal of this particular server is just to have something public-facing to have lay-users try out and to stress test and see what real-world issues may come up. Feel free to do whatever to the <b>Minecraft</b> server; after all, the best way to find bugs is to open it to the public :p</p>
        <p>Logs can be found <a href=/logs>here</a> and are sorted by the commit that was running and the count of each (re)start of the Pumpkin binary. The current instance's logs can be found under the current commit hash directory with the highest number. The current STDOUT log can be found <a href="/logs/{commit}/stdout_{count}.txt">here</a> and the current STDERR log can be found <a href="/logs/{commit}/stderr_{count}.txt">here</a>.</p>
        <br>
        <p>This server instance is also running the latest version of <a href=https://github.com/PumpkinPlugins/CommandLimiter>a permissions plugin</a>.</p>
        <br>
        <p>Pumpkin is currently using: {memory}</p>
        <br>
        <p>Comments about this website? @kralverde can be found at this project's <a href="https://discord.gg/wT8XjrjKkf">discord</a>.</p>
    </div>
  </body>
</html>"""


class MemoryCache:
    def __init__(self, ttl: int = 5):
        self._cache = {}
        self._ttl = ttl
        
    async def get_memory(self, pid: str) -> str:
        now = time.time()
        if pid in self._cache:
            cached_value, timestamp = self._cache[pid]
            if now - timestamp < self._ttl:
                return cached_value
                
        try:
            proc = await asyncio.subprocess.create_subprocess_shell(
                f"ps -o vsz,rss -p {pid}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            exit_code = await proc.wait()
            
            if exit_code != 0:
                raise SubprocessError(
                    await proc.stdout.read() if proc.stdout else None,
                    await proc.stderr.read() if proc.stderr else None,
                )
                
            if proc.stdout:
                await proc.stdout.readline()  # Skip header
                data = await proc.stdout.readline()
                virtual, real = data.strip().decode().split()
                result = f"{int(virtual)//1000}MB of virtual memory and {int(real)//1000}MB of real memory."
                self._cache[pid] = (result, now)
                return result
                
        except Exception as e:
            print(f"Failed to get memory for process: {e}")
            return "Lookup failed"
            
        return "Unknown"


memory_cache = MemoryCache()


async def handle_webhook(queue: asyncio.Queue[str], request: web.Request) -> web.Response:
    if request.headers.get("X-GitHub-Event") == "push":
        raw_data = await request.text()
        json_data = json.loads(urllib.parse.unquote(raw_data))
        repo = json_data["repository"]["full_name"]
        
        if repo in ("kralverde/Pumpkin", "Snowiiii/Pumpkin", "Pumpkin-MC/Pumpkin"):
            print(f'commit detected on {json_data["ref"]}')
            if json_data["ref"] == "refs/heads/master":
                await queue.put(json_data["after"])

    return web.Response()


async def handle_index(
    commit_wrapper: List[Union[str, int]],
    request: web.Request,
) -> web.Response:
    memory_str = "Unknown..."
    if commit_wrapper[4]:
        memory_str = await memory_cache.get_memory(str(commit_wrapper[4]))

    return web.Response(
        body=INDEX_HTML.format(
            commit=commit_wrapper[0],
            count=commit_wrapper[1],
            name=commit_wrapper[2],
            short_commit=str(commit_wrapper[0])[:8],
            error="" if not commit_wrapper[3] else f'<p style="color:red;"><b>{commit_wrapper[3]}</b></p>',
            memory=memory_str
        ),
        content_type="text/html"
    )


async def webhook_runner(
    host: str,
    port: int,
    commit_wrapper: List[Union[str, int]],
    update_queue: asyncio.Queue[str],
) -> None:
    app = web.Application()
    app.add_routes([
        web.post("/watchdog", lambda x: handle_webhook(update_queue, x)),
        web.get("/", lambda x: handle_index(commit_wrapper, x)),
    ])
    print(f"webhook listening at {host}:{port}")
    await web._run_app(app, host=host, port=port, print=lambda _: None)


class MCException(Exception):
    pass


async def mc_read_var_int(reader: asyncio.StreamReader) -> tuple[int, int]:
    value = position = bytes_read = 0
    while True:
        current_byte = (await reader.read(1))[0]
        bytes_read += 1
        value |= (current_byte & 0x7F) << position
        if not (current_byte & 0x80):
            break
        position += 7
        if position >= 32:
            raise MCException("VarInt too big")
    return value, bytes_read


def mc_var_int_length(num: int) -> int:
    length = 0
    while True:
        length += 1
        if not (num & ~0x7F):
            break
        num >>= 7
    return length


def mc_write_var_int(num: int, writer: asyncio.StreamWriter) -> None:
    while True:
        if not (num & ~0x7F):
            writer.write(bytes([num]))
            return
        writer.write(bytes([(num & 0x7F) | 0x80]))
        num >>= 7


async def handle_mc(
    current_error: List[str],
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter
) -> None:
    try:
        packet_length, _ = await mc_read_var_int(reader)
        packet_id, id_bytes = await mc_read_var_int(reader)

        data = {"text": current_error[0]}

        if packet_id == 0:
            protocol_version = await mc_read_var_int(reader)
            server_address_length = await mc_read_var_int(reader)
            if server_address_length[0] > 255:
                raise MCException("Server address is too big")
            _ = await reader.read(server_address_length[0])
            _ = await reader.read(2)
            next_state = await mc_read_var_int(reader)

            real_packet_length = (
                id_bytes +
                protocol_version[1] +
                server_address_length[1] +
                server_address_length[0] +
                2 +
                next_state[1]
            )

            if real_packet_length != packet_length:
                raise MCException(f"Bad packet length ({real_packet_length} vs {packet_length})")

            if next_state[0] == 1:
                await reader.read(2)

                data = {
                    "version": {
                        "name": "Any",
                        "protocol": protocol_version[0],
                    },
                    "players": {
                        "max": 0,
                        "online": 0,
                        "sample": [],
                    },
                    "description": {
                        "text": current_error[0],
                    },
                    "favicon": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAMAAACdt4HsAAAABGdBTUEAALGPC/xhBQAAAAFzUkdCAK7OHOkAAABjUExURQAAAPgxL/MwLfcwLvgwLvcwLvgwLvgxL/gwLvgwL/AvLfcwLu8vLfgxLvgwLusuLOsuK+8vLOwuLPIvLfEvLfMwLvQwLvcwLuwuLPgxL+8vLfcwL+8uLPgwLvgwL/gwL/gxL1kRqKYAAAAgdFJOUwDBT4vBwYn+9vZPwlD2ik9PT1BQUVFQilH2T8JRiMHCyVculgAAAJtJREFUWMPt1skOwjAMBFAXksYu+77T/P9XIijmCNL4wGXmPk9pnUgWYb5m05g1e7w/r6+M0f61vrMDgYkDFxBQB04gUD8hQIAAAQIECBAYYt636IrTg8DRgS26ph2Ca57IOqumKRfuULqslkZ4vy3PKZYW7Z/LcA8KeobkNzGDwMwBjb5G/dcnLPwndugYbsExiiyT6n3FB/UrD1lkOM21nDj+AAAAAElFTkSuQmCC",
                    "enforcesSecureChat": False,
                }

        data_string = json.dumps(data)
        packet_id_length = mc_var_int_length(0)
        data_prefix_length = mc_var_int_length(len(data_string))
        mc_write_var_int(packet_id_length + data_prefix_length + len(data_string), writer)
        mc_write_var_int(0, writer)
        mc_write_var_int(len(data_string), writer)
        writer.write(data_string.encode())
        await writer.drain()
        
    except Exception as e:
        print(f"Failed to handle minecraft: {e}")
        print(traceback.format_exc())
    finally:
        writer.close()


async def deadlock_checker(
    port: int,
    commit_wrapper: List[Union[str, int]],
    can_access_mc: List[bool]
) -> None:
    while True:
        await asyncio.sleep(600)  # 10 minutes
        if not can_access_mc[0]:
            continue
            
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(b"\x06\x00\x00\x00\x00\x00\x01\x01\x00")
            await writer.drain()
            
            try:
                await asyncio.wait_for(reader.read(4096), 10)
                if commit_wrapper[3] == "Deadlock detected!":
                    commit_wrapper[3] = ""
            except asyncio.TimeoutError:
                if can_access_mc[0]:
                    commit_wrapper[3] = "Deadlock detected!"
                    print("Deadlock detected!")
            finally:
                writer.close()
                await writer.wait_closed()
                
        except Exception as e:
            print(f"Error in deadlock checker: {e}")


async def minecraft_runner(
    host: str,
    port: int,
    mc_queue: asyncio.Queue[Optional[str]],
    mc_lock: asyncio.Lock,
) -> None:
    message = ["Booting up"]

    while True:
        await mc_lock.acquire()
        server = None
        
        while True:
            print("starting minecraft notifier")
            try:
                server = await asyncio.start_server(
                    lambda x, y: handle_mc(message, x, y),
                    host,
                    port,
                    reuse_address=True
                )
                break
            except OSError as e:
                print(f"Failed to start minecraft notifier: {e}")
                await asyncio.sleep(300)  # 5 minutes

        if not server:
            continue

        print(f"Serving minecraft notifier on {', '.join(str(sock.getsockname()) for sock in server.sockets)}")
        
        async with server:
            await server.start_serving()
            restart_server = False
            
            while True:
                new_message = await mc_queue.get()
                if new_message is None:
                    if not restart_server:
                        print("stopping minecraft notifier")
                        server.close()
                        await server.wait_closed()
                        mc_lock.release()
                        restart_server = True
                else:
                    message[0] = new_message
                    print(f"updating message to {new_message}")
                    if restart_server:
                        break


async def binary_runner(
    repo_dir: str,
    plugins_dir: str,
    log_dir: str,
    commit_wrapper: List[Union[str, int]],
    update_queue: asyncio.Queue[str],
    mc_queue: asyncio.Queue[Optional[str]],
    mc_lock: asyncio.Lock,
    can_access_mc: List[bool],
) -> None:
    try:
        await mc_queue.put("Updating Repo...")
        await update_git_repo(repo_dir)
    except SubprocessError as e:
        print(f"Warning: Failed to update repo: {e}")

    try:
        await mc_queue.put("Updating Plugin Repos...")
        update_tasks = []
        for path in os.scandir(plugins_dir):
            update_tasks.append(update_git_repo(path.path))
        await asyncio.gather(*update_tasks)
    except SubprocessError as e:
        print(f"Warning: Failed to update plugin repo: {e}")

    while True:
        try:
            await update_rust()
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to update rust!")
            print(f"Warning: Failed to update rust: {e}")
            await asyncio.sleep(600)

    while True:
        try:
            await mc_queue.put("Building Plugins...")
            await build_plugins(repo_dir, plugins_dir)
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to build plugins!")
            print(f"Warning: Failed to build plugins: {e}")
            await asyncio.sleep(600)

    while True:
        try:
            await mc_queue.put("Building Binary...")
            await build_repo(repo_dir, True)
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to build binary!")
            print(f"Warning: Failed to build binary: {e}")
            await asyncio.sleep(600)

    while True:
        try:
            commit, commit_name = await get_repo_description(repo_dir)
            break
        except SubprocessError as e:
            await mc_queue.put("Failed to get commit!")
            print(f"Warning: Failed to get commit: {e}")
            await asyncio.sleep(600)

    print(f"starting commit: {commit}")
    current_log_dir = os.path.join(log_dir, commit)
    os.makedirs(current_log_dir, exist_ok=True)

    executable_path = os.path.join(repo_dir, "./target/release/pumpkin")

    try_counter = max(
        (int(entry.name.replace("stdout_", "").replace(".txt", ""))
        for entry in os.scandir(current_log_dir)
        if entry.name.startswith("stdout_")),
        default=0
    ) + 1

    commit_wrapper[0] = commit
    commit_wrapper[2] = commit_name

    total_counter = 0
    while True:
        total_counter += 1
        commit_wrapper[1] = try_counter
        print(f"attempting to start the binary (try count: {try_counter})")
        
        stdout_path = get_log_file_path(log_dir, commit, try_counter)
        stderr_path = get_log_file_path(log_dir, commit, try_counter, True)

        with open(stdout_path, "w") as stdout_log, open(stderr_path, "w") as stderr_log:
            await mc_queue.put(None)
            await mc_lock.acquire()

            proc = await start_binary(executable_path, stdout_log, stderr_log)
            print("The binary has started")

            can_access_mc[0] = True
            commit_wrapper[3] = ""
            commit_wrapper[4] = proc.pid
            
            try:
                new_commit = await wait_for_process_or_signal(proc, update_queue)
            finally:
                commit_wrapper[4] = ""
                can_access_mc[0] = False
                mc_lock.release()

            if new_commit is None:
                print("The binary died for an unknown reason! Sleeping for 2 minutes.")
                commit_wrapper[3] = "The PumpkinMC binary died for an unknown reason! Check the logs!"
                await mc_queue.put("Binary failure! (Restarting in 5 minutes)")
                await asyncio.sleep(300)
                try_counter += 1
                continue

            try_counter += 1
            print("New commit detected; rebuilding and restarting.")

            try:
                await mc_queue.put("Updating Repo...")
                await update_git_repo(repo_dir)
            except SubprocessError as e:
                print(f"Warning: Failed to update repo: {e}")
                commit_wrapper[3] = f"Unable to update the local repo to {new_commit}"
                continue

            try:
                await mc_queue.put("Updating Plugin Repos...")
                update_tasks = []
                for path in os.scandir(plugins_dir):
                    update_tasks.append(update_git_repo(path.path))
                await asyncio.gather(*update_tasks)
            except SubprocessError as e:
                print(f"Warning: Failed to update plugin repo: {e}")
                commit_wrapper[3] = "Unable to update the plugin repos!"
                continue

            try:
                await update_rust()
            except SubprocessError as e:
                await mc_queue.put("Failed to update rust!")
                print(f"Warning: Failed to update rust: {e}")
                commit_wrapper[3] = f"Unable to update rust for new commit {new_commit}"
                continue

            if total_counter % 50 == 0:
                try:
                    await clean_binary(repo_dir, plugins_dir)
                except SubprocessError as e:
                    print(f"Warning: Failed to clean build dir: {e}")

            try:
                await mc_queue.put("Building Plugins...")
                await build_plugins(repo_dir, plugins_dir)
            except SubprocessError as e:
                await mc_queue.put("Failed to build plugins!")
                print(f"Warning: Failed to build plugins: {e}")
                commit_wrapper[3] = "Unable to build plugins"
                continue

            try:
                await mc_queue.put("Building Binary...")
                await build_repo(repo_dir, True)
            except SubprocessError as e:
                await mc_queue.put("Failed to build binary!")
                print(f"Warning: Failed to build binary: {e}")
                commit_wrapper[3] = f"Unable to build commit {new_commit}"
                continue

            while True:
                try:
                    commit, commit_name = await get_repo_description(repo_dir)
                    break
                except SubprocessError as e:
                    await mc_queue.put("Failed to get commit!")
                    commit_wrapper[3] = "The PumpkinMC binary failed to start!"
                    print(f"Warning: Failed to get commit: {e}")
                    await asyncio.sleep(600)

            print(f"new commit: {commit}")
            current_log_dir = os.path.join(log_dir, commit)
            os.makedirs(current_log_dir, exist_ok=True)

            try_counter = 0
            commit_wrapper[0] = commit
            commit_wrapper[1] = try_counter
            commit_wrapper[2] = commit_name


async def async_main(
    repo_path: str,
    log_path: str,
    plugins_path: str,
    webhook_host: str,
    webhook_port: int,
    mc_host: str,
    mc_port: int,
) -> None:
    update_queue = asyncio.Queue()
    mc_queue = asyncio.Queue()
    mc_lock = asyncio.Lock()

    commit_wrapper = ["0", 0, "default", "", ""]
    can_access_mc = [False]

    tasks = [
        asyncio.create_task(webhook_runner(
            webhook_host, webhook_port, commit_wrapper, update_queue
        )),
        asyncio.create_task(binary_runner(
            repo_path, plugins_path, log_path, commit_wrapper,
            update_queue, mc_queue, mc_lock, can_access_mc
        )),
        asyncio.create_task(minecraft_runner(
            mc_host, mc_port, mc_queue, mc_lock
        )),
        asyncio.create_task(deadlock_checker(
            mc_port, commit_wrapper, can_access_mc
        ))
    ]

    try:
        done, pending = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_EXCEPTION
        )

        for task in done:
            if exc := task.exception():
                raise exc

    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        
        if pending:
            await asyncio.wait(pending)


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: watchdog.py <repo_path> <log_path> <plugins_path>")
        sys.exit(1)
        
    repo_path = os.path.realpath(sys.argv[1])
    log_path = os.path.realpath(sys.argv[2]) 
    plugins_path = os.path.realpath(sys.argv[3])

    asyncio.run(async_main(
        repo_path,
        log_path,
        plugins_path,
        "127.0.0.1",
        8888,
        "0.0.0.0",
        25565
    ))


if __name__ == "__main__":
    main()