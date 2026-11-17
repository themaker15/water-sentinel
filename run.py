"""Start Water Sentinel.

    py run.py              use the ESP32 on USB (auto-detected)
    py run.py --sim        try everything without hardware (built-in simulator)
    py run.py --no-browser start without opening a browser (autostart)
"""
import argparse
import socket
import sys
import threading
import webbrowser


def local_ips():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127."))


def main():
    ap = argparse.ArgumentParser(description="Water Sentinel")
    ap.add_argument("--sim", action="store_true", help="use the built-in simulator instead of the ESP32")
    ap.add_argument("--sim-speed", type=float, default=1.0, help="simulator samples per second")
    ap.add_argument("--host", help="bind address (default: 127.0.0.1, or 0.0.0.0 when remote access is on)")
    ap.add_argument("--port", type=int, help="web port (default from config, 8000)")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    try:
        import uvicorn
        from sentinel.config import load_config
    except ImportError as e:
        sys.exit(f"Missing package ({e.name}). Run:  py -m pip install -r requirements.txt")

    cfg = load_config()
    port = args.port or cfg["http_port"]
    remote = cfg["remote"]["enabled"] and bool(cfg["remote"]["pin"])
    host = args.host or ("0.0.0.0" if remote else "127.0.0.1")
    url = f"http://127.0.0.1:{port}"

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            print(f"Water Sentinel is already running at {url}")
            webbrowser.open(url)
            return

    from sentinel.hub import Hub
    from sentinel.server import create_app

    hub = Hub(sim=args.sim, sim_speed=args.sim_speed)
    hub.start()
    app = create_app(hub)

    print("=" * 60)
    print(f" Water Sentinel  ->  {url}   (keep this window open)")
    if remote:
        for ip in local_ips():
            tag = "  (Tailscale)" if ip.startswith("100.") else ""
            print(f" From phone       ->  http://{ip}:{port}{tag}")
    elif cfg["remote"]["enabled"]:
        print(" Remote access is ON but no PIN is set: staying local-only.")
    print("=" * 60)
    if not args.no_browser:
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        app.state.tunnel.stop()
        hub.shutdown()


if __name__ == "__main__":
    main()
