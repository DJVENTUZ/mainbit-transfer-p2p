"""
Mainbit DataLink v2
Migración de datos entre dos PCs por red local / cable Ethernet directo.

Motor: TCP directo (sin carpetas compartidas, sin credenciales de Windows).
 - Analiza ANTES de enviar: cuenta archivos y peso total, y rechaza si pasa de 500 GB.
 - El receptor valida el límite y el espacio libre antes de aceptar.
 - No sigue enlaces/uniones (evita ciclos infinitos que llenan el disco).
 - Verifica cada archivo con CRC32; escribe a .mbpart y renombra solo si está completo.
 - Conserva fechas de modificación y carpetas vacías (como robocopy /E).
"""
import ctypes
import json
import os
import queue
import shutil
import socket
import stat
import struct
import subprocess
import threading
import time
import zlib
from datetime import datetime

APP_NAME = "Mainbit DataLink"
MAGIC = "MAINBIT2"
PORT_TRANSFER = 50008
PORT_DISCOVER = 50009
CHUNK = 1024 * 1024
PEQUENO = 256 * 1024
LIMITE_BYTES = 500 * 1024 ** 3          # 500 GB (Windows muestra GB en base 1024)
MARGEN_LIBRE = 1024 ** 3                # el receptor exige total + 1 GB libres
EXCLUIR_NOMBRES = {"$recycle.bin", "system volume information", "pagefile.sys",
                   "hiberfil.sys", "swapfile.sys", "thumbs.db"}
EXCLUIR_RAIZ = {"windows", "program files", "program files (x86)", "programdata",
                "$windows.~bt", "$windows.~ws", "windows.old", "recovery",
                "msocache", "config.msi"}
ATTR_REPARSE = 0x400
ATTR_NUBE = 0x1000 | 0x40000 | 0x400000   # archivos "solo en la nube" (OneDrive)


class Cancelado(Exception):
    pass


class Rechazado(Exception):
    pass


class LimiteExcedido(Exception):
    def __init__(self, total):
        super().__init__(total)
        self.total = total


# ----------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------
def lp(path):
    """Prefijo \\\\?\\ en Windows para soportar rutas de más de 260 caracteres."""
    if os.name != "nt":
        return path
    if path.startswith("\\\\?\\"):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def limpio(path):
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def dentro(a, b):
    """True si la ruta a (normalizada) es b o está dentro de b."""
    try:
        return os.path.commonpath([a, b]) == b
    except ValueError:
        return False


def fmt_bytes(n):
    n = float(max(n, 0))
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {u}" if u == "B" else f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.2f} TB"


def fmt_tiempo(s):
    if s is None or s != s or s == float("inf"):
        return "—"
    s = int(s)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def acortar(texto, n=56):
    if len(texto) <= n:
        return texto
    return texto[: n // 2 - 1] + "…" + texto[-(n // 2):]


def recv_exact(sock, n):
    buf = bytearray(n)
    mv = memoryview(buf)
    got = 0
    while got < n:
        k = sock.recv_into(mv[got:], n - got)
        if not k:
            raise ConnectionError("La conexión se cerró antes de tiempo.")
        got += k
    return bytes(buf)


def afinar(sock):
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
    except OSError:
        pass


def cerrar_sock(s):
    for fn in (lambda: s.shutdown(socket.SHUT_RDWR), s.close):
        try:
            fn()
        except OSError:
            pass


def obtener_ips_locales():
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))   # sin gateway falla; por eso no es la única vía
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def ruta_segura(base, rel):
    """Evita que el emisor escriba fuera de la carpeta destino."""
    rel = rel.replace("\\", "/")
    dest = os.path.normpath(os.path.join(base, *rel.split("/")))
    if not dentro(os.path.normcase(dest), os.path.normcase(base)):
        raise ValueError(f"Ruta no permitida: {rel}")
    return dest


def es_peligrosa(ruta):
    k = os.path.normcase(os.path.abspath(ruta))
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"):
        v = os.environ.get(var)
        if v and dentro(k, os.path.normcase(os.path.abspath(v))):
            return True
    return False


def _known_folder(csidl):
    try:
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
            return buf.value
    except Exception:
        pass
    return None


def ruta_escritorio():
    return _known_folder(0x0000) if os.name == "nt" and _known_folder(0x0000) \
        else os.path.join(os.path.expanduser("~"), "Desktop")


def carpetas_usuario():
    """Escritorio, Documentos, Imágenes, Música, Videos y Descargas (las que existan)."""
    home = os.path.expanduser("~")
    res = []
    if os.name == "nt":
        res = [p for p in (_known_folder(c) for c in (0x0000, 0x0005, 0x0027, 0x000D, 0x000E)) if p]
    if not res:
        res = [os.path.join(home, n) for n in ("Desktop", "Documents", "Pictures", "Music", "Videos")]
    res.append(os.path.join(home, "Downloads"))
    out, vistos = [], set()
    for p in res:
        k = os.path.normcase(os.path.abspath(p))
        if os.path.isdir(p) and k not in vistos:
            out.append(p)
            vistos.add(k)
    return out


# --- Firewall / energía (solo Windows) --------------------------------
def es_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _netsh(args):
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall"] + args,
                           capture_output=True, creationflags=0x08000000, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


def abrir_firewall():
    if os.name != "nt" or not es_admin():
        return False
    a = _netsh(["add", "rule", "name=Mainbit DataLink TCP", "dir=in", "action=allow",
                "protocol=TCP", f"localport={PORT_TRANSFER}", "profile=any"])
    b = _netsh(["add", "rule", "name=Mainbit DataLink UDP", "dir=in", "action=allow",
                "protocol=UDP", f"localport={PORT_DISCOVER}", "profile=any"])
    return a and b


def cerrar_firewall():
    if os.name == "nt" and es_admin():
        _netsh(["delete", "rule", "name=Mainbit DataLink TCP"])
        _netsh(["delete", "rule", "name=Mainbit DataLink UDP"])


def evitar_suspension(activar):
    """Impide que Windows se suspenda durante una migración larga."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001 if activar else 0x80000000)
    except Exception:
        pass


# ----------------------------------------------------------------------
# Estado compartido de un trabajo
# ----------------------------------------------------------------------
class Progreso:
    def __init__(self):
        self.fase = "idle"   # idle | analizando | esperando | conectando | transfiriendo | fin | error
        self.files_total = 0
        self.files_done = 0
        self.bytes_total = 0
        self.bytes_done = 0
        self.actual = ""
        self.completo = False


class Job:
    def __init__(self, log):
        self.prog = Progreso()
        self.cancel = threading.Event()
        self.log = log          # log(texto, nivel) nivel: None|"ok"|"warn"|"err"|"info"
        self.socks = []

    def registrar(self, s):
        self.socks.append(s)
        return s

    def abortar(self):
        self.cancel.set()
        for s in list(self.socks):
            cerrar_sock(s)


# ----------------------------------------------------------------------
# Análisis previo (cuenta archivos y peso, aplica el límite)
# ----------------------------------------------------------------------
def nombre_unico(nombre, usados):
    base, n = nombre, 2
    while nombre.lower() in usados:
        nombre = f"{base} ({n})"
        n += 1
    usados.add(nombre.lower())
    return nombre


def escanear(job, rutas, limite=LIMITE_BYTES):
    p = job.prog
    archivos, carpetas, omitidos = [], [], []
    usados = set()
    total = 0
    for ruta in rutas:
        ruta = os.path.abspath(ruta)
        if os.path.isfile(lp(ruta)):
            try:
                st = os.stat(lp(ruta))
            except OSError:
                omitidos.append(("sin acceso", ruta))
                continue
            archivos.append((lp(ruta), nombre_unico(os.path.basename(ruta), usados), st.st_size, st.st_mtime))
            total += st.st_size
            p.files_total += 1
            p.bytes_total = total
            if total > limite:
                raise LimiteExcedido(total)
            continue
        if not os.path.isdir(lp(ruta)):
            omitidos.append(("no existe", ruta))
            continue

        es_raiz = os.path.dirname(ruta) == ruta
        base = os.path.basename(os.path.normpath(ruta)) or ruta.replace(":", "").strip("\\/") or "RAIZ"
        top = nombre_unico(base, usados)
        carpetas.append(top)
        pila = [(ruta, top)]
        while pila:
            if job.cancel.is_set():
                raise Cancelado()
            d, rel = pila.pop()
            try:
                it = os.scandir(lp(d))
            except OSError:
                omitidos.append(("sin acceso a la carpeta", d))
                continue
            with it:
                while True:
                    try:
                        e = next(it)
                    except StopIteration:
                        break
                    except OSError:
                        omitidos.append(("error leyendo carpeta", d))
                        break
                    low = e.name.lower()
                    if low in EXCLUIR_NOMBRES:
                        continue
                    if es_raiz and d == ruta and low in EXCLUIR_RAIZ:
                        continue
                    rel_e = f"{rel}/{e.name}"
                    try:
                        if e.is_symlink():
                            omitidos.append(("enlace simbólico", e.path))
                            continue
                        st = e.stat(follow_symlinks=False)
                        attrs = getattr(st, "st_file_attributes", 0)
                        if e.is_dir(follow_symlinks=False):
                            if attrs & ATTR_REPARSE:
                                omitidos.append(("unión/enlace de carpeta", e.path))
                                continue
                            carpetas.append(rel_e)
                            pila.append((e.path, rel_e))
                        elif e.is_file(follow_symlinks=False):
                            if attrs & ATTR_NUBE:
                                omitidos.append(("solo en la nube (no descargado)", e.path))
                                continue
                            archivos.append((e.path, rel_e, st.st_size, st.st_mtime))
                            total += st.st_size
                            p.files_total += 1
                            p.bytes_total = total
                            if total > limite:
                                raise LimiteExcedido(total)
                    except OSError:
                        omitidos.append(("sin acceso", e.path))
    return {"archivos": archivos, "carpetas": carpetas, "bytes": total, "omitidos": omitidos}


# ----------------------------------------------------------------------
# EMISOR
# ----------------------------------------------------------------------
def buscar_receptor():
    for ip in obtener_ips_locales() or [""]:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            s.settimeout(1.5)
            dests = ["255.255.255.255"]
            if ip.startswith("169.254."):
                dests.append("169.254.255.255")
            elif ip:
                dests.append(ip.rsplit(".", 1)[0] + ".255")
            for d in dests:
                try:
                    s.sendto(b"MAINBIT_DISCOVER", (d, PORT_DISCOVER))
                except OSError:
                    pass
            while True:
                data, addr = s.recvfrom(1024)
                if data.startswith(b"MAINBIT_HERE"):
                    host = data.split(b"|", 1)[1].decode("utf-8", "replace") if b"|" in data else ""
                    return addr[0], host
        except OSError:
            pass
        finally:
            s.close()
    return None


def enviar(job, ip, scan, port=PORT_TRANSFER):
    p = job.prog
    archivos, carpetas, omitidos = scan["archivos"], scan["carpetas"], scan["omitidos"]
    p.fase = "conectando"
    job.log(f"Conectando con {ip}…", None)
    s = job.registrar(socket.create_connection((ip, port), timeout=10))
    try:
        afinar(s)
        s.settimeout(120)
        head = json.dumps({"magic": MAGIC, "files": len(archivos), "dirs": len(carpetas),
                           "bytes": scan["bytes"], "skipped": len(omitidos),
                           "host": socket.gethostname()}).encode("utf-8")
        s.sendall(struct.pack("!I", len(head)) + head)
        st, n = struct.unpack("!cH", recv_exact(s, 3))
        msg = recv_exact(s, n).decode("utf-8", "replace") if n else ""
        if st != b"A":
            raise Rechazado(msg or "El receptor rechazó la transferencia.")

        p.fase = "transfiriendo"
        p.files_total, p.bytes_total = len(archivos), scan["bytes"]
        p.files_done = p.bytes_done = 0
        job.log("El receptor aceptó. Transfiriendo…", "ok")

        lote = bytearray()
        for rel in carpetas:
            pb = rel.encode("utf-8")
            lote += b"D" + struct.pack("!I", len(pb)) + pb
            if len(lote) > 65536:
                s.sendall(lote)
                lote.clear()
        if lote:
            s.sendall(lote)

        buf = bytearray(CHUNK)
        mv = memoryview(buf)
        fallidos, ilegibles = [], []
        top_prev = None
        for ruta, rel, size, mtime in archivos:
            if job.cancel.is_set():
                raise Cancelado()
            try:
                f = open(ruta, "rb")
            except OSError as e:
                fallidos.append((rel, e.strerror or str(e)))
                p.files_total -= 1
                p.bytes_total -= size
                if len(fallidos) <= 100:
                    job.log(f"No se pudo abrir: {acortar(rel)} ({e.strerror or e})", "warn")
                continue
            top = rel.split("/", 1)[0]
            if top != top_prev:
                top_prev = top
                job.log(f"Carpeta: {top}", "info")
            p.actual = rel
            pb = rel.encode("utf-8")
            cab = b"F" + struct.pack("!I", len(pb)) + pb + struct.pack("!Qd", size, float(mtime))
            buenas = True
            with f:
                if size <= PEQUENO:
                    try:
                        data = f.read(size)
                    except OSError:
                        data = b""
                    if len(data) < size:
                        buenas = False
                        data += bytes(size - len(data))
                    crc = zlib.crc32(data)
                    if not buenas:
                        crc ^= 0xFFFFFFFF
                    s.sendall(cab + data + struct.pack("!I", crc & 0xFFFFFFFF))
                    p.bytes_done += size
                else:
                    s.sendall(cab)
                    crc, restante = 0, size
                    while restante:
                        want = min(CHUNK, restante)
                        n = 0
                        if buenas:
                            try:
                                n = f.readinto(mv[:want])
                            except OSError:
                                n = 0
                        if not n:   # error de lectura o archivo que se encogió
                            buenas = False
                            relleno = bytes(want)
                            s.sendall(relleno)
                            crc = zlib.crc32(relleno, crc)
                            n = want
                        else:
                            s.sendall(mv[:n])
                            crc = zlib.crc32(mv[:n], crc)
                        restante -= n
                        p.bytes_done += n
                        if job.cancel.is_set():
                            raise Cancelado()
                    if not buenas:
                        crc ^= 0xFFFFFFFF   # CRC erróneo a propósito: el receptor descarta el archivo
                    s.sendall(struct.pack("!I", crc & 0xFFFFFFFF))
            if not buenas:
                ilegibles.append(rel)
                job.log(f"Ilegible o modificado durante el envío: {acortar(rel)}", "warn")
            p.files_done += 1

        notas = [f"{m} | {limpio(r)}" for m, r in omitidos]
        notas += [f"no se pudo abrir | {r} ({m})" for r, m in fallidos]
        notas += [f"ilegible o modificado durante el envío | {r}" for r in ilegibles]
        texto = "\n".join(notas[:5000]).encode("utf-8")
        s.sendall(b"S" + struct.pack("!I", len(texto)) + texto + b"E")

        st, ok, bad = struct.unpack("!cII", recv_exact(s, 9))
        return {"enviados": p.files_done, "fallidos": fallidos, "ilegibles": ilegibles,
                "omitidos": omitidos, "recibidos_ok": ok, "recibidos_mal": bad}
    finally:
        cerrar_sock(s)


# ----------------------------------------------------------------------
# RECEPTOR
# ----------------------------------------------------------------------
def servidor_discovery(stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("", PORT_DISCOVER))
        sock.settimeout(1.0)
        resp = b"MAINBIT_HERE|" + socket.gethostname().encode("utf-8", "replace")
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(1024)
            except socket.timeout:
                continue
            if data == b"MAINBIT_DISCOVER":
                sock.sendto(resp, addr)
    except OSError:
        pass
    finally:
        sock.close()


def _rechazar(conn, motivo):
    try:
        b = motivo.encode("utf-8")
        conn.sendall(struct.pack("!cH", b"R", len(b)) + b)
    except OSError:
        pass


def escribir_reporte(destino, d):
    L = [f"{APP_NAME} — reporte de migración",
         f"Fecha: {datetime.now():%Y-%m-%d %H:%M:%S}",
         f"Emisor: {d['host']} ({d['ip']})",
         f"Archivos recibidos y verificados: {d['ok']:,}",
         f"Datos: {fmt_bytes(d['bytes'])}",
         f"Duración: {fmt_tiempo(d['seg'])}",
         f"Archivos con verificación fallida (descartados): {len(d['malos'])}"]
    L += [f"   - {r} ({m})" for r, m in d["malos"][:2000]]
    L += ["", "Notas del emisor (omitidos / no leídos):", d["notas"] or "   (ninguna)"]
    ruta = os.path.join(destino, "_REPORTE_MIGRACION.txt")
    try:
        with open(lp(ruta), "w", encoding="utf-8") as f:
            f.write("\n".join(L))
        return ruta
    except OSError:
        return None


def recibir(job, destino, port=PORT_TRANSFER):
    p = job.prog
    destino = os.path.abspath(destino)
    os.makedirs(lp(destino), exist_ok=True)

    srv = job.registrar(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
    if os.name != "nt":
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port))
    srv.listen(1)
    srv.settimeout(1.0)
    p.fase = "esperando"

    while True:   # espera una migración válida (ignora conexiones raras o rechazadas)
        if job.cancel.is_set():
            raise Cancelado()
        try:
            conn, addr = srv.accept()
        except socket.timeout:
            continue
        job.registrar(conn)
        try:
            conn.settimeout(15)
            afinar(conn)
            hlen = struct.unpack("!I", recv_exact(conn, 4))[0]
            if hlen > 1_000_000:
                raise ValueError("cabecera")
            head = json.loads(recv_exact(conn, hlen).decode("utf-8"))
            if head.get("magic") != MAGIC:
                raise ValueError("magic")
            total, nfiles = int(head["bytes"]), int(head["files"])
        except (OSError, ValueError, KeyError, struct.error):
            job.log(f"Conexión no válida desde {addr[0]} (ignorada).", "warn")
            cerrar_sock(conn)
            continue

        motivo = None
        if total > LIMITE_BYTES:
            motivo = f"La migración ({fmt_bytes(total)}) supera el límite de {fmt_bytes(LIMITE_BYTES)}."
        else:
            libre = shutil.disk_usage(destino).free
            if libre < total + MARGEN_LIBRE:
                motivo = (f"Espacio insuficiente en el receptor: se necesitan "
                          f"{fmt_bytes(total + MARGEN_LIBRE)} y hay {fmt_bytes(libre)} libres.")
        if motivo:
            _rechazar(conn, motivo)
            job.log(f"Migración de {addr[0]} rechazada: {motivo}", "err")
            cerrar_sock(conn)
            continue
        conn.sendall(struct.pack("!cH", b"A", 0))
        break

    cerrar_sock(srv)
    host = str(head.get("host", "?"))
    job.log(f"Conectado con {host} ({addr[0]}): {nfiles:,} archivos · {fmt_bytes(total)}", "ok")
    p.fase = "transfiriendo"
    p.files_total, p.bytes_total = nfiles, total
    t0 = time.monotonic()
    ok, malos, notas = 0, [], ""
    buf = bytearray(CHUNK)
    mv = memoryview(buf)
    top_prev = None
    try:
        conn.settimeout(120)
        while True:
            if job.cancel.is_set():
                raise Cancelado()
            tipo = recv_exact(conn, 1)
            if tipo == b"E":
                break
            n = struct.unpack("!I", recv_exact(conn, 4))[0]
            datos = recv_exact(conn, n)
            if tipo == b"S":
                notas = datos.decode("utf-8", "replace")
                continue
            rel = datos.decode("utf-8")
            dest = ruta_segura(destino, rel)
            if tipo == b"D":
                os.makedirs(lp(dest), exist_ok=True)
                continue
            if tipo != b"F":
                raise ValueError("Protocolo inválido.")
            size, mtime = struct.unpack("!Qd", recv_exact(conn, 16))
            p.actual = rel
            top = rel.split("/", 1)[0]
            if top != top_prev:
                top_prev = top
                job.log(f"Carpeta: {top}", "info")

            final = lp(dest)
            os.makedirs(os.path.dirname(final), exist_ok=True)
            tmp = final + ".mbpart"
            crc, restante = 0, size
            try:
                with open(tmp, "wb") as f:
                    while restante:
                        k = conn.recv_into(mv[:min(CHUNK, restante)])
                        if not k:
                            raise ConnectionError("Conexión interrumpida durante un archivo.")
                        f.write(mv[:k])
                        crc = zlib.crc32(mv[:k], crc)
                        restante -= k
                        p.bytes_done += k
                crc_rx = struct.unpack("!I", recv_exact(conn, 4))[0]
                if crc_rx != (crc & 0xFFFFFFFF):
                    os.remove(tmp)
                    malos.append((rel, "verificación CRC fallida u origen ilegible"))
                    job.log(f"Verificación fallida (descartado): {acortar(rel)}", "err")
                else:
                    if os.path.exists(final):
                        try:
                            os.chmod(final, stat.S_IWRITE)
                        except OSError:
                            pass
                    os.replace(tmp, final)
                    try:
                        os.utime(final, (mtime, mtime))
                    except (OSError, OverflowError, ValueError):
                        pass
                    ok += 1
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            p.files_done += 1

        seg = time.monotonic() - t0
        rep = escribir_reporte(destino, {"host": host, "ip": addr[0], "ok": ok, "malos": malos,
                                         "bytes": p.bytes_done, "seg": seg, "notas": notas})
        conn.sendall(struct.pack("!cII", b"K" if not malos else b"X", ok, len(malos)))
        return {"ok": ok, "malos": malos, "host": host, "ip": addr[0], "bytes": p.bytes_done,
                "seg": seg, "reporte": rep, "notas": notas}
    finally:
        cerrar_sock(conn)


# ===== INTERFAZ (Tkinter) =====
import tkinter as tk
from tkinter import filedialog, messagebox, font as tkfont

C = {
    "bg": "#000000", "card": "#1C1C1E", "fill": "#2C2C2E", "gray": "#3A3A3C",
    "label": "#FFFFFF", "label2": "#8E8E93", "blue": "#0A84FF", "green": "#30D158",
    "red": "#FF453A", "yellow": "#FFD60A", "disabled": "#2C2C2E",
}
COL = {"ok": C["green"], "warn": C["yellow"], "err": C["red"], "info": C["blue"], None: C["label2"]}


def round_rect(canvas, x1, y1, x2, y2, r, **kw):
    r = min(r, (x2 - x1) / 2, (y2 - y1) / 2)
    pts = [x1 + r, y1, x1 + r, y1, x2 - r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
           x2, y1 + r, x2, y2 - r, x2, y2 - r, x2, y2, x2 - r, y2, x2 - r, y2,
           x1 + r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y2 - r, x1, y1 + r,
           x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def oscurecer(hex_color, f):
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return "#%02x%02x%02x" % tuple(min(255, int(c * f)) for c in (r, g, b))


class RoundedButton(tk.Canvas):
    def __init__(self, parent, text, command, bg, fg="#FFFFFF", fondo_padre=None,
                 font=None, height=40, width=100, radius=13):
        super().__init__(parent, height=height, width=width, bg=fondo_padre,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.color, self.fg = text, command, bg, fg
        self.font, self.radius, self.enabled, self._actual = font, radius, True, bg
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        fill = self._actual if self.enabled else C["disabled"]
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline="")
        self.create_text(w // 2, h // 2, text=self.text, font=self.font,
                         fill=self.fg if self.enabled else "#636366")

    def _press(self, _e):
        if self.enabled:
            self._actual = oscurecer(self.color, 0.75)
            self._dibujar()

    def _release(self, e):
        if not self.enabled:
            return
        self._actual = self.color
        self._dibujar()
        if 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def set_enabled(self, valor):
        self.enabled = valor
        self.configure(cursor="hand2" if valor else "arrow")
        self._dibujar()


class Card(tk.Canvas):
    def __init__(self, parent, fondo_padre, fill=None, radius=18, pad=14, width=380):
        super().__init__(parent, bg=fondo_padre, highlightthickness=0, bd=0, width=width, height=10)
        self.fill, self.radius, self.pad = fill or C["card"], radius, pad
        self.inner = tk.Frame(self, bg=self.fill)
        self.win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._relayout)
        self.inner.bind("<Configure>", self._relayout)

    def _relayout(self, _e=None):
        w = self.winfo_width()
        h = self.inner.winfo_reqheight() + 2 * self.pad
        if int(float(self.cget("height"))) != h:
            self.config(height=h)
        self.delete("bg")
        round_rect(self, 0, 0, w, h, self.radius, fill=self.fill, outline="", tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self.win, width=max(1, w - 2 * self.pad))


class Capsula(tk.Canvas):
    def __init__(self, parent, fondo_padre):
        super().__init__(parent, height=8, bg=fondo_padre, highlightthickness=0, bd=0)
        self.valor = 0
        self.bind("<Configure>", lambda e: self._dibujar())

    def set(self, v):
        v = max(0, min(100, v))
        if v != self.valor:
            self.valor = v
            self._dibujar()

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), 8
        round_rect(self, 0, 0, w, h, 4, fill=C["fill"], outline="")
        if self.valor > 0:
            round_rect(self, 0, 0, max(h, int(w * self.valor / 100)), h, 4, fill=C["blue"], outline="")


class Segmentado(tk.Canvas):
    def __init__(self, parent, opciones, comando, fondo_padre, font, width=300, height=34):
        super().__init__(parent, width=width, height=height, bg=fondo_padre,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.opciones, self.comando, self.font = opciones, comando, font
        self.sel, self.activo = 0, True
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<Button-1>", self._click)

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        n = len(self.opciones)
        sw = w / n
        round_rect(self, 0, 0, w, h, 10, fill=C["fill"], outline="")
        round_rect(self, self.sel * sw + 2, 2, self.sel * sw + sw - 2, h - 2, 8, fill=C["gray"], outline="")
        for i, t in enumerate(self.opciones):
            self.create_text(sw * i + sw / 2, h / 2, text=t, font=self.font,
                             fill=C["label"] if self.activo or i == self.sel else "#636366")

    def _click(self, e):
        if not self.activo:
            return
        i = max(0, min(len(self.opciones) - 1, int(e.x // (self.winfo_width() / len(self.opciones)))))
        if i != self.sel:
            self.sel = i
            self._dibujar()
            self.comando(i)


def titulo_oscuro(root):
    try:
        root.update()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        v = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), 4) == 0:
                break
        negro = ctypes.c_int(0x000000)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 35, ctypes.byref(negro), 4)
    except Exception:
        pass


class App:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.configure(bg=C["bg"])
        root.resizable(False, False)
        self.sc = max(1.0, root.winfo_fpixels("1i") / 96.0)
        fams = set(tkfont.families())
        self.F = next((f for f in ("SF Pro Display", "SF Pro Text", "Segoe UI Variable Display",
                                   "Segoe UI Variable Text", "Segoe UI") if f in fams), "Helvetica")
        self.q = queue.Queue()
        self.job = None
        self.ocupado = False
        self.rutas = []
        self.destino = os.path.join(ruta_escritorio(), "BACKUP_MIGRACION")
        self.fw_abierto = False
        self._vel, self._t_last, self._b_last = 0.0, time.monotonic(), 0
        self.btns = []
        self.construir()
        titulo_oscuro(root)
        root.attributes("-alpha", 0.92)
        root.protocol("WM_DELETE_WINDOW", self.cerrar)
        self.cambiar_modo(0)
        root.after(150, self.tick)

    def S(self, n):
        return int(n * self.sc)

    # ---------------- construcción ----------------
    def seccion(self, padre, texto):
        tk.Label(padre, text=texto.upper(), font=(self.F, 9), bg=C["bg"], fg=C["label2"],
                 anchor="w").pack(fill="x", padx=10, pady=(10, 4))

    def boton(self, padre, texto, cmd, color, fg="#FFFFFF", h=38, w=100, bold=True, track=True):
        b = RoundedButton(padre, texto, cmd, color, fg=fg, fondo_padre=C["card"],
                          font=(self.F, 9, "bold" if bold else "normal"), height=h, width=w)
        if track:
            self.btns.append(b)
        return b

    def construir(self):
        F = self.F
        cab = tk.Frame(self.root, bg=C["bg"])
        cab.pack(fill="x", padx=24, pady=(16, 4))
        tk.Label(cab, text="DataLink", font=(F, 24, "bold"), bg=C["bg"], fg=C["label"]).pack(side="left")
        tk.Label(cab, text="MAINBIT", font=(F, 8, "bold"), bg=C["fill"], fg=C["blue"],
                 padx=9, pady=3).pack(side="left", padx=10)
        self.seg = Segmentado(cab, ["Receptor · PC nueva", "Emisor · PC vieja"], self.cambiar_modo,
                              C["bg"], (F, 9, "bold"), width=self.S(320))
        self.seg.pack(side="right")

        cuerpo = tk.Frame(self.root, bg=C["bg"])
        cuerpo.pack(fill="both", expand=True, padx=14)
        cuerpo.columnconfigure((0, 1), weight=1, uniform="col")
        self.col_izq = tk.Frame(cuerpo, bg=C["bg"])
        self.col_der = tk.Frame(cuerpo, bg=C["bg"])
        self.col_izq.grid(row=0, column=0, sticky="new", padx=(0, 6))
        self.col_der.grid(row=0, column=1, sticky="new", padx=(6, 0))

        self.panel_rec = tk.Frame(self.col_izq, bg=C["bg"])
        self.panel_env = tk.Frame(self.col_izq, bg=C["bg"])
        self.construir_receptor()
        self.construir_emisor()
        self.construir_actividad()

        pie = tk.Frame(self.root, bg=C["bg"])
        pie.pack(fill="x", padx=24, pady=(8, 12))
        tk.Label(pie, text="Desarrollado para Mainbit por Gabriel Hernández Arroyo",
                 font=(F, 8), bg=C["bg"], fg="#636366").pack(side="left")
        esc = tk.Scale(pie, from_=60, to=100, orient="horizontal", showvalue=0, length=self.S(120),
                       command=lambda v: self.root.attributes("-alpha", int(float(v)) / 100),
                       bg=C["bg"], troughcolor=C["fill"], highlightthickness=0, bd=0,
                       sliderrelief="flat", activebackground=C["blue"], width=10)
        esc.set(92)
        esc.pack(side="right")
        tk.Label(pie, text="Transparencia", font=(F, 8), bg=C["bg"], fg=C["label2"]).pack(side="right", padx=6)

    def construir_receptor(self):
        F = self.F
        self.seccion(self.panel_rec, "Este equipo · Receptor")
        card = Card(self.panel_rec, C["bg"], width=self.S(430))
        card.pack(fill="x")
        a = card.inner
        tk.Label(a, text="Dirección IP de este equipo", font=(F, 9), bg=C["card"], fg=C["label2"],
                 anchor="w").pack(fill="x")
        self.lbl_ips = tk.Label(a, text="—", font=(F, 20, "bold"), bg=C["card"], fg=C["blue"],
                                anchor="w", justify="left")
        self.lbl_ips.pack(fill="x", pady=(0, 2))
        tk.Label(a, text="Escríbela en el emisor o usa «Auto-buscar».", font=(F, 8), bg=C["card"],
                 fg=C["label2"], anchor="w").pack(fill="x", pady=(0, 10))
        tk.Label(a, text="Carpeta de destino", font=(F, 9), bg=C["card"], fg=C["label2"],
                 anchor="w").pack(fill="x")
        fila = tk.Frame(a, bg=C["card"])
        fila.pack(fill="x", pady=(2, 10))
        self.lbl_dest = tk.Label(fila, text=self.destino, font=(F, 9), bg=C["card"], fg=C["label"],
                                 anchor="w", justify="left", wraplength=self.S(240))
        self.lbl_dest.pack(side="left", fill="x", expand=True)
        self.boton(fila, "Cambiar", self.cambiar_destino, C["fill"], C["blue"], h=32, w=80).pack(side="right")
        self.boton(a, "Activar receptor", self.iniciar_receptor, C["blue"], h=44).pack(fill="x")
        self.lbl_fw = tk.Label(a, text="Si Windows pregunta por el Firewall, pulsa «Permitir».\n"
                                       "Ejecutar como administrador abre los puertos automáticamente.",
                               font=(F, 8), bg=C["card"], fg=C["label2"], anchor="w", justify="left",
                               wraplength=self.S(400))
        self.lbl_fw.pack(fill="x", pady=(8, 0))
        self.lbl_ips.config(text="\n".join(obtener_ips_locales()) or "No detectada")

    def construir_emisor(self):
        F = self.F
        self.seccion(self.panel_env, "Este equipo · Emisor")
        card = Card(self.panel_env, C["bg"], width=self.S(430))
        card.pack(fill="x")
        a = card.inner
        tk.Label(a, text="IP de la PC nueva", font=(F, 9), bg=C["card"], fg=C["label2"],
                 anchor="w").pack(fill="x", pady=(0, 3))
        fila = tk.Frame(a, bg=C["card"])
        fila.pack(fill="x")
        self.entry_ip = tk.Entry(fila, font=(F, 11), bg=C["fill"], fg=C["label"],
                                 insertbackground=C["label"], relief="flat", bd=0,
                                 disabledbackground=C["fill"], disabledforeground=C["label2"])
        self.entry_ip.pack(side="left", fill="x", expand=True, ipady=9, padx=(0, 8))
        self.btn_buscar = self.boton(fila, "Auto-buscar", self.buscar, C["gray"], h=36, w=100)
        self.btn_buscar.pack(side="right")

        g = tk.Frame(a, bg=C["card"])
        g.pack(fill="x", pady=(10, 6))
        g.columnconfigure((0, 1), weight=1, uniform="b")
        for i, (t, cmd) in enumerate((("Perfil de usuario", self.add_perfil),
                                      ("+ Varias carpetas", self.add_varias),
                                      ("+ Carpeta", self.add_carpeta),
                                      ("+ Archivos", self.add_archivos))):
            self.boton(g, t, cmd, C["fill"], C["blue"], h=34).grid(row=i // 2, column=i % 2,
                                                                  sticky="ew", padx=3, pady=3)
        marco = tk.Frame(a, bg=C["fill"])
        marco.pack(fill="x")
        self.listbox = tk.Listbox(marco, height=6, bg=C["fill"], fg=C["label"], font=(F, 9),
                                  selectbackground=C["blue"], relief="flat", highlightthickness=0,
                                  bd=0, activestyle="none", selectmode="extended")
        self.listbox.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        sb = tk.Scrollbar(marco, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        f2 = tk.Frame(a, bg=C["card"])
        f2.pack(fill="x", pady=(6, 0))
        self.lbl_resumen = tk.Label(f2, text="0 elementos seleccionados", font=(F, 9), bg=C["card"],
                                    fg=C["label2"], anchor="w")
        self.lbl_resumen.pack(side="left")
        self.boton(f2, "Quitar", self.quitar, C["card"], C["red"], h=28, w=70).pack(side="right")

        f3 = tk.Frame(a, bg=C["card"])
        f3.pack(fill="x", pady=(8, 0))
        f3.columnconfigure(0, weight=1, uniform="e")
        f3.columnconfigure(1, weight=2, uniform="e")
        self.boton(f3, "Analizar", lambda: self.accion_emisor(True), C["gray"], h=42).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        self.boton(f3, "Enviar", lambda: self.accion_emisor(False), C["green"], h=42).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

    def construir_actividad(self):
        F = self.F
        self.seccion(self.col_der, "Actividad")
        card = Card(self.col_der, C["bg"], width=self.S(430))
        card.pack(fill="x")
        a = card.inner
        fila = tk.Frame(a, bg=C["card"])
        fila.pack(fill="x")
        self.lbl_pct = tk.Label(fila, text="0%", font=(F, 28, "bold"), bg=C["card"], fg=C["label"],
                                width=5, anchor="w")
        self.lbl_pct.pack(side="left")
        self.lbl_estado = tk.Label(fila, text="Listo. Elige el rol de este equipo.", font=(F, 10),
                                   bg=C["card"], fg=C["label2"], anchor="w", justify="left",
                                   wraplength=self.S(230))
        self.lbl_estado.pack(side="left", fill="x", expand=True)
        self.progress = Capsula(a, C["card"])
        self.progress.pack(fill="x", pady=(6, 8))

        st = tk.Frame(a, bg=C["card"])
        st.pack(fill="x")
        st.columnconfigure((0, 1), weight=1, uniform="s")
        self.vals = {}
        for i, (k, cap) in enumerate((("arch", "ARCHIVOS"), ("datos", "DATOS"),
                                      ("vel", "VELOCIDAD"), ("rest", "TIEMPO RESTANTE"))):
            r, c = (i // 2) * 2, i % 2
            tk.Label(st, text=cap, font=(F, 7), bg=C["card"], fg=C["label2"], anchor="w").grid(
                row=r, column=c, sticky="w")
            lb = tk.Label(st, text="—", font=(F, 10, "bold"), bg=C["card"], fg=C["label"], anchor="w")
            lb.grid(row=r + 1, column=c, sticky="w", pady=(0, 4))
            self.vals[k] = lb

        self.lbl_actual = tk.Label(a, text="", font=(F, 8), bg=C["card"], fg=C["label2"], anchor="w",
                                   justify="left", wraplength=self.S(400))
        self.lbl_actual.pack(fill="x", pady=(2, 6))
        self.txt = tk.Text(a, height=10, bg=C["fill"], fg=C["label"], font=(F, 8), relief="flat",
                           highlightthickness=0, bd=0, wrap="word", state="disabled", padx=8, pady=6)
        for k, v in COL.items():
            self.txt.tag_config(str(k), foreground=v)
        self.txt.pack(fill="x")
        self.btn_cancel = RoundedButton(a, "Cancelar", self.cancelar, C["fill"], fg=C["red"],
                                        fondo_padre=C["card"], font=(F, 10, "bold"), height=38)
        self.btn_cancel.pack(fill="x", pady=(10, 0))
        self.btn_cancel.set_enabled(False)

    # ---------------- modo / ventana ----------------
    def cambiar_modo(self, i):
        self.panel_rec.pack_forget()
        self.panel_env.pack_forget()
        (self.panel_rec if i == 0 else self.panel_env).pack(fill="x")
        self.ajustar_ventana()

    def ajustar_ventana(self):
        self.root.update_idletasks()
        self.root.geometry(f"{max(self.S(940), self.root.winfo_reqwidth())}x{self.root.winfo_reqheight()}")

    def set_ocupado(self, v):
        self.ocupado = v
        self.seg.activo = not v
        self.seg._dibujar()
        for b in self.btns:
            b.set_enabled(not v)
        self.btn_cancel.set_enabled(v)
        self.entry_ip.config(state="disabled" if v else "normal")
        evitar_suspension(v)

    # ---------------- cola de mensajes hilo → UI ----------------
    def log(self, texto, nivel=None):
        self.q.put(("log", (texto, nivel)))

    def estado(self, texto, nivel=None):
        self.q.put(("estado", (texto, nivel)))

    def msg(self, tipo, titulo, texto):
        self.q.put(("msg", (tipo, titulo, texto)))

    def tick(self):
        try:
            for _ in range(200):
                try:
                    kind, d = self.q.get_nowait()
                except queue.Empty:
                    break
                if kind == "log":
                    self._log(*d)
                elif kind == "estado":
                    self.lbl_estado.config(text=d[0], fg=COL.get(d[1], C["label2"]))
                elif kind == "resumen":
                    self.lbl_resumen.config(text=d[0])
                elif kind == "ip":
                    self.entry_ip.config(state="normal")
                    self.entry_ip.delete(0, tk.END)
                    self.entry_ip.insert(0, d[0])
                elif kind == "msg":
                    getattr(messagebox, d[0])(d[1], d[2])
                elif kind == "fin":
                    self.set_ocupado(False)
            self.refrescar()
        except Exception as e:   # la UI nunca debe morir por un error de refresco
            print("tick:", e)
        finally:
            self.root.after(150, self.tick)

    def _log(self, texto, nivel):
        self.txt.config(state="normal")
        self.txt.insert(tk.END, f"{datetime.now():%H:%M:%S}  {texto}\n", str(nivel))
        if int(self.txt.index("end-1c").split(".")[0]) > 600:
            self.txt.delete("1.0", "150.0")
        self.txt.see(tk.END)
        self.txt.config(state="disabled")

    def refrescar(self):
        if not self.job:
            return
        p = self.job.prog
        now = time.monotonic()
        if p.fase == "analizando":
            self.lbl_pct.config(text="…")
            self.progress.set(0)
            self.vals["arch"].config(text=f"{p.files_total:,}")
            self.vals["datos"].config(text=fmt_bytes(p.bytes_total))
            self.vals["vel"].config(text="—")
            self.vals["rest"].config(text="—")
            self.lbl_actual.config(text="Analizando carpetas…")
        elif p.fase in ("transfiriendo", "fin"):
            if p.completo:
                pct = 100
            elif p.bytes_total > 0:
                pct = min(99, int(min(p.bytes_done, p.bytes_total) * 100 / p.bytes_total))
            else:
                pct = 0
            rest = None
            if p.fase == "transfiriendo":
                dt = now - self._t_last
                if dt >= 0.5:
                    inst = (p.bytes_done - self._b_last) / dt
                    self._vel = inst if self._vel == 0 else 0.7 * self._vel + 0.3 * inst
                    self._t_last, self._b_last = now, p.bytes_done
                if self._vel > 1:
                    rest = (p.bytes_total - p.bytes_done) / self._vel
                self.vals["vel"].config(text=f"{fmt_bytes(self._vel)}/s")
                self.lbl_actual.config(text=acortar(p.actual, 70))
            else:
                self.vals["vel"].config(text="—")
                self.lbl_actual.config(text="")
            self.lbl_pct.config(text=f"{pct}%")
            self.progress.set(pct)
            self.vals["arch"].config(text=f"{p.files_done:,} / {p.files_total:,}")
            self.vals["datos"].config(text=f"{fmt_bytes(p.bytes_done)} / {fmt_bytes(p.bytes_total)}")
            self.vals["rest"].config(text=fmt_tiempo(rest) if p.fase == "transfiriendo" else "—")

    def nuevo_job(self):
        self.job = Job(self.log)
        self._vel, self._t_last, self._b_last = 0.0, time.monotonic(), 0
        self.txt.config(state="normal")
        self.txt.delete("1.0", tk.END)
        self.txt.config(state="disabled")
        self.lbl_pct.config(text="0%")
        self.progress.set(0)
        self.lbl_actual.config(text="")
        for k in self.vals:
            self.vals[k].config(text="—")
        self.set_ocupado(True)

    def cancelar(self):
        if self.job and self.ocupado:
            self.log("Cancelando…", "warn")
            self.job.abortar()

    def cerrar(self):
        if self.ocupado:
            if not messagebox.askyesno("Cerrar", "Hay una transferencia en curso. ¿Cancelarla y salir?"):
                return
            self.job.abortar()
        if self.fw_abierto:
            cerrar_firewall()
        evitar_suspension(False)
        self.root.destroy()

    # ---------------- selección (emisor) ----------------
    def agregar_ruta(self, ruta):
        ruta = os.path.abspath(ruta)
        if es_peligrosa(ruta):
            messagebox.showwarning("Carpeta del sistema", f"No se migran carpetas del sistema:\n{ruta}")
            return False
        k = os.path.normcase(ruta)
        if any(dentro(k, os.path.normcase(r)) for r in self.rutas):
            return False   # ya está cubierta por otra selección
        self.rutas = [r for r in self.rutas if not dentro(os.path.normcase(r), k)]
        self.rutas.append(ruta)
        return True

    def refrescar_lista(self):
        self.listbox.delete(0, tk.END)
        for r in self.rutas:
            self.listbox.insert(tk.END, ("📄 " if os.path.isfile(r) else "📁 ") + r)
        self.lbl_resumen.config(text=f"{len(self.rutas)} elementos seleccionados · pulsa Analizar")

    def add_carpeta(self):
        d = filedialog.askdirectory(title="Selecciona una carpeta de origen")
        if d and self.agregar_ruta(d):
            self.refrescar_lista()

    def add_archivos(self):
        n = sum(self.agregar_ruta(f) for f in filedialog.askopenfilenames(title="Selecciona archivos"))
        if n:
            self.refrescar_lista()

    def add_perfil(self):
        n = sum(self.agregar_ruta(c) for c in carpetas_usuario())
        self.refrescar_lista()
        self.log(f"Perfil de usuario: {n} carpetas agregadas.", "info")

    def add_varias(self):
        padre = filedialog.askdirectory(title="Carpeta que contiene las carpetas a enviar")
        if not padre:
            return
        try:
            subs = sorted((e.name for e in os.scandir(padre) if e.is_dir(follow_symlinks=False)),
                          key=str.lower)
        except OSError as e:
            messagebox.showerror("Error", f"No se pudo leer la carpeta:\n{e}")
            return
        if not subs:
            messagebox.showinfo("Sin subcarpetas", "Esa carpeta no tiene subcarpetas.")
            return
        win = tk.Toplevel(self.root)
        win.title("Selecciona carpetas")
        win.configure(bg=C["card"])
        win.transient(self.root)
        win.grab_set()
        tk.Label(win, text="Ctrl / Shift para elegir varias", font=(self.F, 9), bg=C["card"],
                 fg=C["label2"]).pack(padx=14, pady=(12, 4), anchor="w")
        lb = tk.Listbox(win, selectmode="extended", height=14, width=48, bg=C["fill"], fg=C["label"],
                        font=(self.F, 10), selectbackground=C["blue"], relief="flat",
                        highlightthickness=0, bd=0, activestyle="none")
        for s in subs:
            lb.insert(tk.END, s)
        lb.pack(padx=14, pady=4, fill="both", expand=True)

        def agregar():
            n = sum(self.agregar_ruta(os.path.join(padre, subs[i])) for i in lb.curselection())
            win.destroy()
            self.refrescar_lista()
            self.log(f"{n} carpetas agregadas desde {os.path.basename(padre) or padre}.", "info")

        fila = tk.Frame(win, bg=C["card"])
        fila.pack(fill="x", padx=14, pady=(4, 12))
        RoundedButton(fila, "Seleccionar todo", lambda: lb.select_set(0, tk.END), C["fill"], C["blue"],
                      C["card"], (self.F, 9, "bold"), 36, 130).pack(side="left")
        RoundedButton(fila, "Agregar", agregar, C["blue"], "#FFFFFF", C["card"],
                      (self.F, 9, "bold"), 36, 110).pack(side="right")
        RoundedButton(fila, "Cancelar", win.destroy, C["fill"], C["red"], C["card"],
                      (self.F, 9, "bold"), 36, 100).pack(side="right", padx=8)

    def quitar(self):
        for i in reversed(self.listbox.curselection()):
            del self.rutas[i]
        self.refrescar_lista()

    def cambiar_destino(self):
        d = filedialog.askdirectory(title="Carpeta de destino de la migración")
        if d:
            self.destino = os.path.join(d, "BACKUP_MIGRACION") if os.path.basename(d).lower() != "backup_migracion" else d
            self.lbl_dest.config(text=self.destino)

    # ---------------- receptor ----------------
    def iniciar_receptor(self):
        self.nuevo_job()
        self.lbl_ips.config(text="\n".join(obtener_ips_locales()) or "No detectada")
        threading.Thread(target=self._hilo_receptor, args=(self.job, self.destino), daemon=True).start()

    def _hilo_receptor(self, job, destino):
        stop = threading.Event()
        threading.Thread(target=servidor_discovery, args=(stop,), daemon=True).start()
        p = job.prog
        try:
            if abrir_firewall():
                self.fw_abierto = True
                job.log("Firewall: puertos abiertos automáticamente.", "ok")
            ips = ", ".join(obtener_ips_locales()) or "sin IP"
            job.log(f"Receptor activo · IP {ips} · puerto {PORT_TRANSFER}", "info")
            job.log(f"Destino: {destino}", None)
            self.estado("Esperando al emisor…", "info")
            res = recibir(job, destino)
            p.fase, p.completo = "fin", True
            malos = res["malos"]
            self.estado("¡Migración completada!" if not malos else "Completada con advertencias",
                        "ok" if not malos else "warn")
            job.log(f"Listo: {res['ok']:,} archivos verificados en {fmt_tiempo(res['seg'])}.", "ok")
            texto = (f"{res['ok']:,} archivos recibidos y verificados ({fmt_bytes(res['bytes'])}) "
                     f"en {fmt_tiempo(res['seg'])}.\n\nCarpeta:\n{destino}\n\nReporte: _REPORTE_MIGRACION.txt")
            if malos:
                self.msg("showwarning", "Migración con advertencias",
                         texto + f"\n\n{len(malos)} archivos fallaron la verificación y se descartaron.")
            else:
                self.msg("showinfo", "Migración finalizada", texto)
        except Cancelado:
            p.fase = "error"
            self.estado("Receptor detenido.", "warn")
            job.log("Receptor detenido por el usuario.", "warn")
        except Exception as e:
            p.fase = "error"
            if job.cancel.is_set():
                self.estado("Receptor detenido.", "warn")
                job.log("Receptor detenido por el usuario.", "warn")
            else:
                self.estado("Error en el receptor", "err")
                job.log(f"Error: {e}", "err")
                self.msg("showerror", "Error en el receptor", f"{e}")
        finally:
            stop.set()
            job.abortar()
            self.q.put(("fin", ()))

    # ---------------- emisor ----------------
    def buscar(self):
        self.estado("Buscando receptor en la red local…", "warn")
        self.btn_buscar.set_enabled(False)

        def run():
            r = buscar_receptor()
            if r:
                self.q.put(("ip", (r[0],)))
                self.estado(f"Receptor encontrado: {r[1] or r[0]} ({r[0]})", "ok")
                self.log(f"Receptor encontrado: {r[1]} en {r[0]}", "ok")
            else:
                self.estado("Sin respuesta del receptor", "err")
                self.msg("showwarning", "Auto-buscar",
                         "No se encontró el receptor.\n\nRevisa: cable conectado, receptor activo, "
                         "Firewall (red Privada) o escribe la IP a mano.")
            self.root.after(0, lambda: self.btn_buscar.set_enabled(not self.ocupado))
        threading.Thread(target=run, daemon=True).start()

    def accion_emisor(self, solo_analizar):
        if not self.rutas:
            messagebox.showwarning("Aviso", "Agrega al menos una carpeta o archivo.")
            return
        ip = self.entry_ip.get().strip()
        if not solo_analizar:
            try:
                socket.inet_pton(socket.AF_INET, ip)
            except OSError:
                messagebox.showerror("IP no válida", "Escribe la IP de la PC nueva (ej. 192.168.1.20) "
                                                     "o usa «Auto-buscar».")
                return
        self.nuevo_job()
        threading.Thread(target=self._hilo_emisor, args=(self.job, ip, list(self.rutas), solo_analizar),
                         daemon=True).start()

    def _hilo_emisor(self, job, ip, rutas, solo_analizar):
        p = job.prog
        try:
            p.fase = "analizando"
            self.estado("Analizando archivos…", "warn")
            job.log("Analizando selección…", None)
            scan = escanear(job, rutas)
            n, b = len(scan["archivos"]), scan["bytes"]
            om = scan["omitidos"]
            job.log(f"Análisis: {n:,} archivos · {len(scan['carpetas']):,} carpetas · {fmt_bytes(b)}", "ok")
            for motivo, ruta in om[:40]:
                job.log(f"Omitido ({motivo}): {acortar(limpio(ruta), 60)}", "warn")
            if len(om) > 40:
                job.log(f"…y {len(om) - 40:,} omitidos más (irán en el reporte del receptor).", "warn")
            self.q.put(("resumen", (f"{n:,} archivos · {fmt_bytes(b)}",)))
            if n == 0:
                raise ValueError("No hay archivos legibles para enviar.")
            if solo_analizar:
                p.fase, p.completo = "fin", True
                self.estado(f"Listo para enviar: {n:,} archivos · {fmt_bytes(b)}", "ok")
                return
            res = enviar(job, ip, scan)
            p.fase, p.completo = "fin", True
            problemas = len(res["fallidos"]) + len(res["ilegibles"]) + res["recibidos_mal"]
            self.estado("¡Envío completado!" if not problemas else "Completado con advertencias",
                        "ok" if not problemas else "warn")
            job.log(f"Enviados {res['enviados']:,} · verificados por el receptor {res['recibidos_ok']:,}.", "ok")
            texto = (f"{res['enviados']:,} archivos enviados ({fmt_bytes(p.bytes_done)}).\n"
                     f"Verificados por el receptor: {res['recibidos_ok']:,}.\n"
                     f"Omitidos en el análisis: {len(res['omitidos']):,}.")
            if problemas:
                self.msg("showwarning", "Envío con advertencias",
                         texto + f"\n\nCon problemas: {problemas:,} (ver el reporte en la PC nueva: "
                                 f"_REPORTE_MIGRACION.txt).")
            else:
                self.msg("showinfo", "Envío finalizado", texto)
        except Cancelado:
            p.fase = "error"
            self.estado("Operación cancelada.", "warn")
            job.log("Cancelado por el usuario.", "warn")
        except LimiteExcedido as e:
            p.fase = "error"
            self.estado("Denegado: supera el límite", "err")
            job.log(f"DENEGADO: la selección supera {fmt_bytes(LIMITE_BYTES)} (va en {fmt_bytes(e.total)}).", "err")
            self.msg("showwarning", "Selección demasiado grande",
                     f"La selección supera el límite de {fmt_bytes(LIMITE_BYTES)} "
                     f"(ya lleva {fmt_bytes(e.total)}).\n\nQuita carpetas o divide la migración en partes.")
        except Rechazado as e:
            p.fase = "error"
            self.estado("El receptor rechazó la migración", "err")
            job.log(f"Rechazado por el receptor: {e}", "err")
            self.msg("showwarning", "Migración rechazada", str(e))
        except Exception as e:
            fase = p.fase
            p.fase = "error"
            if job.cancel.is_set():
                self.estado("Operación cancelada.", "warn")
                job.log("Cancelado por el usuario.", "warn")
            elif fase == "conectando":
                self.estado("No se pudo conectar", "err")
                job.log(f"No se pudo conectar con {ip}: {e}", "err")
                self.msg("showerror", "No se pudo conectar",
                         f"No hay respuesta de {ip}.\n\nRevisa que el receptor esté activo, el cable, "
                         f"la IP y el Firewall (red Privada).")
            else:
                self.estado("Error en la transferencia", "err")
                job.log(f"Error: {e}", "err")
                self.msg("showerror", "Error en la transferencia", str(e))
        finally:
            job.abortar()
            self.q.put(("fin", ()))


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    raiz = tk.Tk()
    App(raiz)
    raiz.mainloop()
