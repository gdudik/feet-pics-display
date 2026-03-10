from flask import Flask, send_from_directory, jsonify, render_template_string, abort
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import threading
import time
from zeroconf import Zeroconf, ServiceInfo, IPVersion
import socket
import qrcode
import logging
import ipaddress

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

IMAGE_FOLDER = None
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
PORT = 9999

latest_image = None
latest_lock = threading.Lock()

def get_advertisable_ipv4_addresses():
    addresses = set()

    # Collect IPv4s known for this host
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM):
        ip = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip)
        except ValueError:
            continue

        # Keep only sane private LAN addresses
        if ip_obj.is_private and not ip_obj.is_loopback and not ip_obj.is_link_local:
            addresses.add(ip)

    # Also include the currently preferred outbound IPv4, in case hostname lookup misses it
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private and not ip_obj.is_loopback and not ip_obj.is_link_local:
            addresses.add(ip)
    except OSError:
        pass
    finally:
        s.close()

    return sorted(addresses)

def print_qr(url):
    qr = qrcode.QRCode(border=1)
    qr.add_data(url)
    qr.make(fit=True)

    matrix = qr.get_matrix()
    print('Scan to load displayer on mobile device\n')

    for row in matrix:
        print("".join("██" if cell else "  " for cell in row))
    print(f"\nURL {url}\n")


def start_mdns_service(hostname, port=PORT):
    ipv4_addresses = get_advertisable_ipv4_addresses()

    if not ipv4_addresses:
        raise RuntimeError("No suitable private IPv4 addresses found to advertise via mDNS.")

    print("Advertising mDNS on these IPv4 addresses:")
    for ip in ipv4_addresses:
        print(f"  {ip}")

    # Bind zeroconf to only the interfaces/IPs we actually want
    zeroconf = Zeroconf(
        interfaces=ipv4_addresses,
        ip_version=IPVersion.V4Only,
    )

    info = ServiceInfo(
        "_http._tcp.local.",
        f"{hostname}._http._tcp.local.",
        addresses=[socket.inet_aton(ip) for ip in ipv4_addresses],
        port=port,
        properties={},
        server=f"{hostname}.local.",
    )

    zeroconf.register_service(info)
    print(f"mDNS service advertised: http://{hostname}.local:{port}")

    return zeroconf, info

def is_image_file(path_str):
    p = Path(path_str)
    return p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS


def set_latest_image(path_str):
    global latest_image
    p = Path(path_str)

    if p.suffix.lower() not in IMAGE_EXTENSIONS:
        return
    
    for _ in range (10):
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                with latest_lock:
                    latest_image = p.name
                    print(f"Latest image set to: {latest_image}")
                return
        except (PermissionError, FileNotFoundError):
            pass

        time.sleep(0.2)

    # if not is_image_file(p):
    #     return

    # with latest_lock:
    #     latest_image = p.name
    #     print(f"Latest image set to: {latest_image}")


class ImageHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        set_latest_image(event.src_path)

    def on_moved(self, event):
        if event.is_directory:
            return
        set_latest_image(event.dest_path)


def start_watcher():
    event_handler = ImageHandler()
    observer = Observer()
    observer.schedule(event_handler, str(IMAGE_FOLDER), recursive=False)
    observer.start()
    return observer


@app.route("/")
def index():
    return render_template_string("""
<!doctype html>
<html>
<head>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>Latest Image</title>
    <style>
        html, body {
            margin: 0;
            padding: 0;
            background: black;
            width: 100%;
            height: 100%;
            overflow: hidden;
        }
        #wrap {
            width: 100vw;
            height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        #img {
            max-width: 100vw;
            max-height: 100vh;
            object-fit: contain;
        }
        #msg {
            color: white;
            font-family: Arial, sans-serif;
            font-size: 24px;
        }
    </style>
</head>
<body>
    <div id="wrap">
        <div id="msg">Loading...</div>
        <img id="img" style="display:none;" />
    </div>

    <script>
        async function refreshImage() {
            try {
                const res = await fetch('/latest-image-info');
                const data = await res.json();

                const img = document.getElementById('img');
                const msg = document.getElementById('msg');

                if (!data.exists) {
                    img.style.display = 'none';
                    msg.style.display = 'block';
                    msg.textContent = 'No image available';
                    return;
                }

                img.src = '/image/' + encodeURIComponent(data.filename) + '?t=' + Date.now();
                img.style.display = 'block';
                msg.style.display = 'none';
            } catch (err) {
                console.error(err);
            }
        }

        refreshImage();
        setInterval(refreshImage, 2000);
    </script>
</body>
</html>
    """)


@app.route("/latest-image-info")
def latest_image_info():
    with latest_lock:
        if latest_image is None:
            return jsonify({"exists": False})
        return jsonify({"exists": True, "filename": latest_image})


@app.route("/image/<path:filename>")
def serve_image(filename):
    file_path = IMAGE_FOLDER / filename
    if not file_path.exists() or not file_path.is_file():
        abort(404)
    return send_from_directory(IMAGE_FOLDER, filename)


if __name__ == "__main__":
    IMAGE_FOLDER = Path(input("Paste folder containing images to be displayed: "))
    IMAGE_FOLDER.mkdir(parents=True, exist_ok=True)
    hostname = input("Host name to create? Server will be accessible at 'example.local:9999': ")

    observer = start_watcher()

    zeroconf, service_info = start_mdns_service(hostname, port=PORT)
    url = f"http://{hostname}.local:{PORT}"
    print_qr(url)

    try:
        app.run(host="0.0.0.0", port=PORT, debug=True, use_reloader=False)
    finally:
        observer.stop()
        observer.join()
        zeroconf.unregister_service(service_info)
        zeroconf.close()