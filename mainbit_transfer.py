import ctypes
import os
import socket
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, font as tkfont

PORT_TRANSFER = 50008
PORT_DISCOVER = 50009
CHUNK = 128 * 1024

# Paleta estilo iOS (modo oscuro)
C = {
    "bg": "#000000",
    "card": "#1C1C1E",
    "fill": "#2C2C2E",
    "label": "#FFFFFF",
    "label2": "#8E8E93",
    "blue": "#0A84FF",
    "green": "#30D158",
    "red": "#FF453A",
    "yellow": "#FFD60A",
    "gray": "#3A3A3C",
    "disabled": "#2C2C2E",
}


# ----------------------------------------------------------------------
# Utilidades de red
# ----------------------------------------------------------------------
def recv_exact(sock, n):
    """Lee exactamente n bytes (recv() puede devolver menos de lo pedido)."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("La conexión se cerró antes de tiempo.")
        buf.extend(chunk)
    return bytes(buf)


def obtener_ips_locales():
    """Devuelve todas las IPv4 locales (funciona también con cable directo/APIPA)."""
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
        s.connect(("8.8.8.8", 80))  # Falla sin gateway; por eso no es la única vía
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ips)


def ruta_segura(base, rel):
    """Evita que un emisor escriba fuera de la carpeta de respaldo (../)."""
    rel = rel.replace("\\", "/")
    dest = os.path.normpath(os.path.join(base, *rel.split("/")))
    if os.path.commonpath([base, dest]) != base:
        raise ValueError(f"Ruta no permitida: {rel}")
    return dest


# ----------------------------------------------------------------------
# Componentes visuales estilo iOS
# ----------------------------------------------------------------------
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
                 font=None, height=44, width=100, radius=14):
        super().__init__(parent, height=height, width=width, bg=fondo_padre,
                         highlightthickness=0, bd=0, cursor="hand2")
        self.text, self.command, self.color, self.fg = text, command, bg, fg
        self.font, self.radius, self.enabled = font, radius, True
        self._actual = bg
        self.bind("<Configure>", lambda e: self._dibujar())
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), self.winfo_height()
        fill = self._actual if self.enabled else C["disabled"]
        round_rect(self, 1, 1, w - 1, h - 1, self.radius, fill=fill, outline="")
        self.create_text(w // 2, h // 2, text=self.text, font=self.font,
                         fill=self.fg if self.enabled else C["label2"])

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
    """Tarjeta redondeada (estilo grouped list de iOS). Contenido en .inner"""

    def __init__(self, parent, fondo_padre, fill=None, radius=18, pad=14):
        super().__init__(parent, bg=fondo_padre, highlightthickness=0, bd=0)
        self.fill, self.radius, self.pad = fill or C["card"], radius, pad
        self.inner = tk.Frame(self, bg=self.fill)
        self.win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._relayout)
        self.inner.bind("<Configure>", self._relayout)

    def _relayout(self, _e=None):
        w = self.winfo_width()
        h = self.inner.winfo_reqheight() + 2 * self.pad
        if int(self.cget("height")) != h:
            self.config(height=h)
        self.delete("bg")
        round_rect(self, 0, 0, w, h, self.radius, fill=self.fill, outline="", tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self.win, width=max(1, w - 2 * self.pad))


class Capsula(tk.Canvas):
    """Barra de progreso tipo cápsula."""

    def __init__(self, parent, fondo_padre):
        super().__init__(parent, height=8, bg=fondo_padre, highlightthickness=0, bd=0)
        self.valor = 0
        self.bind("<Configure>", lambda e: self._dibujar())

    def set(self, v):
        self.valor = max(0, min(100, v))
        self._dibujar()

    def _dibujar(self):
        self.delete("all")
        w, h = self.winfo_width(), 8
        round_rect(self, 0, 0, w, h, 4, fill=C["fill"], outline="")
        if self.valor > 0:
            round_rect(self, 0, 0, max(h, int(w * self.valor / 100)), h, 4,
                       fill=C["blue"], outline="")


# ----------------------------------------------------------------------
# Aplicación
# ----------------------------------------------------------------------
class TransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mainbit DataLink")
        self.root.config(bg=C["bg"])
        self.root.resizable(False, False)
        try:
            self.root.attributes("-alpha", 0.98)
        except tk.TclError:
            pass

        fams = set(tkfont.families())
        self.F = next((f for f in ("SF Pro Display", "SF Pro Text",
                                   "Segoe UI Variable Display", "Segoe UI Variable Text",
                                   "Segoe UI") if f in fams), "Helvetica")

        self.rutas_seleccionadas = []
        self.stop_discovery = threading.Event()
        self.crear_interfaz()

        self.root.update_idletasks()
        self.root.geometry(f"480x{self.root.winfo_reqheight()}")

    # ---------------- helpers UI ----------------
    def ui(self, fn, *args, **kw):
        """Ejecuta fn en el hilo de la interfaz (seguro desde hilos secundarios)."""
        self.root.after(0, lambda: fn(*args, **kw))

    def set_estado(self, texto, color=None):
        self.lbl_estado.config(text=texto, fg=color or C["label2"])

    def set_progreso(self, pct):
        self.progress.set(pct)
        self.lbl_pct.config(text=f"{pct}%")

    def seccion(self, texto):
        tk.Label(self.root, text=texto.upper(), font=(self.F, 9), bg=C["bg"],
                 fg=C["label2"], anchor="w").pack(fill="x", padx=30, pady=(12, 4))

    def crear_interfaz(self):
        F = self.F
        # --- Encabezado (Large Title) ---
        head = tk.Frame(self.root, bg=C["bg"])
        head.pack(fill="x", padx=24, pady=(22, 4))
        tk.Label(head, text="DataLink", font=(F, 26, "bold"), bg=C["bg"],
                 fg=C["label"]).pack(side="left")
        tk.Label(head, text="MAINBIT", font=(F, 9, "bold"), bg=C["fill"],
                 fg=C["blue"], padx=10, pady=4).pack(side="right")

        # --- Estado ---
        card_est = Card(self.root, C["bg"], radius=14, pad=12)
        card_est.pack(fill="x", padx=20, pady=(8, 4))
        self.lbl_estado = tk.Label(card_est.inner, text="Listo. Elige el rol de este equipo.",
                                   font=(F, 10), bg=C["card"], fg=C["label2"],
                                   anchor="w", justify="left", wraplength=400)
        self.lbl_estado.pack(fill="x")

        # --- Receptor ---
        self.seccion("PC nueva · Receptor")
        card_rec = Card(self.root, C["bg"])
        card_rec.pack(fill="x", padx=20)
        self.btn_servidor = RoundedButton(
            card_rec.inner, "Activar modo receptor", self.iniciar_servidor_hilo,
            C["blue"], fondo_padre=C["card"], font=(F, 11, "bold"))
        self.btn_servidor.pack(fill="x")

        # --- Emisor ---
        self.seccion("PC vieja · Emisor")
        card_env = Card(self.root, C["bg"])
        card_env.pack(fill="x", padx=20)
        inn = card_env.inner

        tk.Label(inn, text="IP de la PC nueva", font=(F, 9), bg=C["card"],
                 fg=C["label2"], anchor="w").pack(fill="x", pady=(0, 4))
        fila_ip = tk.Frame(inn, bg=C["card"])
        fila_ip.pack(fill="x")
        self.entry_ip = tk.Entry(fila_ip, font=(F, 11), bg=C["fill"], fg=C["label"],
                                 insertbackground=C["label"], relief="flat", bd=0)
        self.entry_ip.pack(side="left", fill="x", expand=True, ipady=10, padx=(0, 8))
        self.btn_buscar = RoundedButton(fila_ip, "Auto-buscar", self.buscar_pc_nueva,
                                        C["gray"], fondo_padre=C["card"],
                                        font=(F, 9, "bold"), height=38, width=100, radius=12)
        self.btn_buscar.pack(side="right")

        fila_sel = tk.Frame(inn, bg=C["card"])
        fila_sel.pack(fill="x", pady=(10, 8))
        for i in range(3):
            fila_sel.columnconfigure(i, weight=1, uniform="b")
        for i, (txt, cmd, col) in enumerate((
                ("+ Carpeta", self.agregar_carpeta, C["fill"]),
                ("+ Archivos", self.agregar_archivos, C["fill"]),
                ("Quitar", self.quitar_item, C["fill"]))):
            b = RoundedButton(fila_sel, txt, cmd, col,
                              fg=C["red"] if txt == "Quitar" else C["blue"],
                              fondo_padre=C["card"], font=(F, 9, "bold"), height=36, radius=12)
            b.grid(row=0, column=i, sticky="ew", padx=3)

        marco_lista = tk.Frame(inn, bg=C["fill"])
        marco_lista.pack(fill="x")
        self.listbox = tk.Listbox(marco_lista, height=4, bg=C["fill"], fg=C["label"],
                                  font=(F, 9), selectbackground=C["blue"],
                                  relief="flat", highlightthickness=0, bd=0, activestyle="none")
        self.listbox.pack(side="left", fill="both", expand=True, padx=8, pady=6)
        sb = tk.Scrollbar(marco_lista, orient="vertical", command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=sb.set)

        self.btn_enviar = RoundedButton(inn, "Iniciar transferencia por Ethernet",
                                        self.iniciar_envio_hilo, C["green"],
                                        fondo_padre=C["card"], font=(F, 11, "bold"))
        self.btn_enviar.pack(fill="x", pady=(10, 0))

        # --- Progreso ---
        fila_prog = tk.Frame(self.root, bg=C["bg"])
        fila_prog.pack(fill="x", padx=30, pady=(16, 4))
        self.progress = Capsula(fila_prog, C["bg"])
        self.progress.pack(side="left", fill="x", expand=True)
        self.lbl_pct = tk.Label(fila_prog, text="0%", font=(F, 9, "bold"), width=5,
                                bg=C["bg"], fg=C["label2"])
        self.lbl_pct.pack(side="right")

        tk.Label(self.root, text="Desarrollado para Mainbit por Gabriel Hernández Arroyo",
                 font=(F, 8), bg=C["bg"], fg="#636366").pack(pady=(8, 14))

    # ---------------- selección ----------------
    def agregar_carpeta(self):
        d = filedialog.askdirectory(title="Selecciona carpeta de origen")
        if d and d not in self.rutas_seleccionadas:
            self.rutas_seleccionadas.append(d)
            self.listbox.insert(tk.END, f"[Carpeta] {d}")

    def agregar_archivos(self):
        for f in filedialog.askopenfilenames(title="Selecciona archivos sueltos"):
            if f and f not in self.rutas_seleccionadas:
                self.rutas_seleccionadas.append(f)
                self.listbox.insert(tk.END, f"[Archivo] {f}")

    def quitar_item(self):
        sel = self.listbox.curselection()
        if sel:
            self.listbox.delete(sel[0])
            del self.rutas_seleccionadas[sel[0]]

    # ---------------- RECEPTOR ----------------
    def iniciar_servidor_hilo(self):
        ips = obtener_ips_locales()
        texto = " / ".join(ips) if ips else "no detectada"
        self.set_estado(f"Receptor activo. IP: {texto}", C["blue"])
        self.btn_servidor.set_enabled(False)
        self.set_progreso(0)
        self.stop_discovery.clear()
        threading.Thread(target=self.ejecutar_servidor_archivos, daemon=True).start()
        threading.Thread(target=self.ejecutar_servidor_discovery, daemon=True).start()

    def ejecutar_servidor_discovery(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("", PORT_DISCOVER))
            sock.settimeout(1.0)
            while not self.stop_discovery.is_set():
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    continue
                if data == b"MAINBIT_DISCOVER":
                    sock.sendto(b"MAINBIT_HERE", addr)
        except OSError:
            pass
        finally:
            sock.close()

    def ejecutar_servidor_archivos(self):
        backup_dir = os.path.abspath(
            os.path.join(os.path.expanduser("~"), "Desktop", "BACKUP_MIGRACION"))
        server = None
        try:
            os.makedirs(backup_dir, exist_ok=True)
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", PORT_TRANSFER))
            server.listen(1)

            conn, addr = server.accept()
            conn.settimeout(60)
            with conn:
                total = struct.unpack("!Q", recv_exact(conn, 8))[0]
                self.ui(self.set_estado, f"Conectado con {addr[0]}. Transfiriendo...", C["yellow"])

                recibidos, ultimo = 0, -1
                while True:
                    plen = struct.unpack("!I", recv_exact(conn, 4))[0]
                    if plen == 0:
                        break
                    rel = recv_exact(conn, plen).decode("utf-8")
                    size = struct.unpack("!Q", recv_exact(conn, 8))[0]

                    dest = ruta_segura(backup_dir, rel)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)

                    restante = size
                    with open(dest, "wb") as f:
                        while restante > 0:
                            chunk = conn.recv(min(CHUNK, restante))
                            if not chunk:
                                raise ConnectionError(f"Conexión interrumpida en: {rel}")
                            f.write(chunk)
                            restante -= len(chunk)
                            recibidos += len(chunk)
                            pct = int(recibidos * 100 / total) if total else 0
                            if pct != ultimo:
                                ultimo = pct
                                self.ui(self.set_progreso, pct)

                conn.sendall(b"OK")  # Confirmación para el emisor

            self.ui(self.set_progreso, 100)
            self.ui(self.set_estado, "¡Migración completada!", C["green"])
            self.ui(messagebox.showinfo, "Éxito",
                    f"¡Migración finalizada!\nDirectorio de respaldo:\n{backup_dir}")
        except Exception as e:
            msg = str(e)
            self.ui(self.set_estado, "Error en el receptor", C["red"])
            self.ui(messagebox.showerror, "Error crítico", f"Fallo en el receptor:\n{msg}")
        finally:
            self.stop_discovery.set()
            if server:
                server.close()
            self.ui(self.btn_servidor.set_enabled, True)

    # ---------------- EMISOR ----------------
    def buscar_pc_nueva(self):
        self.set_estado("Buscando equipo receptor en la red local...", C["yellow"])
        self.btn_buscar.set_enabled(False)
        threading.Thread(target=self._escanear, daemon=True).start()

    def _escanear(self):
        encontrada = None
        for ip in obtener_ips_locales() or [""]:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind((ip, 0))  # Sale por cada interfaz, incluido el cable directo
                s.settimeout(1.5)
                s.sendto(b"MAINBIT_DISCOVER", ("255.255.255.255", PORT_DISCOVER))
                while True:
                    data, addr = s.recvfrom(1024)
                    if data == b"MAINBIT_HERE":
                        encontrada = addr[0]
                        break
            except OSError:
                pass
            finally:
                s.close()
            if encontrada:
                break

        if encontrada:
            self.ui(self.entry_ip.delete, 0, tk.END)
            self.ui(self.entry_ip.insert, 0, encontrada)
            self.ui(self.set_estado, f"Enlazado con {encontrada}", C["green"])
        else:
            self.ui(self.set_estado, "Sin respuesta del receptor", C["red"])
            self.ui(messagebox.showwarning, "Aviso",
                    "No se detectó al receptor. Verifica el cable, que el receptor esté "
                    "activo y que la red esté como 'Privada'. También puedes escribir la IP a mano.")
        self.ui(self.btn_buscar.set_enabled, True)

    def iniciar_envio_hilo(self):
        ip = self.entry_ip.get().strip()
        if not ip:
            messagebox.showerror("Error", "Ingresa o busca la IP de la PC nueva.")
            return
        if not self.rutas_seleccionadas:
            messagebox.showwarning("Aviso", "Agrega al menos un archivo o carpeta.")
            return
        self.btn_enviar.set_enabled(False)
        self.set_progreso(0)
        self.set_estado("Analizando archivos y transmitiendo...", C["yellow"])
        threading.Thread(target=self.ejecutar_envio,
                         args=(ip, list(self.rutas_seleccionadas)), daemon=True).start()

    def ejecutar_envio(self, ip, rutas):
        s = None
        try:
            archivos, omitidos, total = [], 0, 0

            def registrar(full, rel):
                nonlocal omitidos, total
                try:
                    size = os.path.getsize(full)
                    with open(full, "rb"):
                        pass
                except OSError:
                    omitidos += 1  # bloqueado / sin permisos
                    return
                archivos.append((full, rel.replace(os.sep, "/"), size))
                total += size

            for ruta in rutas:
                if os.path.isfile(ruta):
                    registrar(ruta, os.path.basename(ruta))
                elif os.path.isdir(ruta):
                    raiz = (os.path.basename(os.path.normpath(ruta))
                            or ruta.rstrip(":\\/").replace(":", "") or "RAIZ")
                    for carpeta, _dirs, files in os.walk(ruta):
                        for nombre in files:
                            full = os.path.join(carpeta, nombre)
                            registrar(full, os.path.join(raiz, os.path.relpath(full, ruta)))

            if not archivos:
                raise ValueError("No hay archivos legibles para enviar.")

            s = socket.create_connection((ip, PORT_TRANSFER), timeout=10)
            s.settimeout(60)
            s.sendall(struct.pack("!Q", total))

            enviados, ultimo = 0, -1
            for full, rel, size in archivos:
                pb = rel.encode("utf-8")
                s.sendall(struct.pack("!I", len(pb)) + pb + struct.pack("!Q", size))

                restante = size
                with open(full, "rb") as f:
                    while restante > 0:
                        chunk = f.read(min(CHUNK, restante))
                        if not chunk:
                            raise IOError(f"El archivo cambió durante el envío: {rel}")
                        s.sendall(chunk)
                        restante -= len(chunk)
                        enviados += len(chunk)
                        pct = int(enviados * 100 / total) if total else 0
                        if pct != ultimo:
                            ultimo = pct
                            self.ui(self.set_progreso, pct)

            s.sendall(struct.pack("!I", 0))
            s.settimeout(30)
            if recv_exact(s, 2) != b"OK":
                raise ConnectionError("El receptor no confirmó la recepción.")

            self.ui(self.set_progreso, 100)
            self.ui(self.set_estado, "¡Envío completado!", C["green"])
            extra = f"\n\nArchivos omitidos (bloqueados): {omitidos}" if omitidos else ""
            self.ui(messagebox.showinfo, "Éxito",
                    f"¡{len(archivos)} archivos transferidos a la PC nueva!{extra}")
        except Exception as e:
            msg = str(e)
            self.ui(self.set_estado, "Error crítico en red", C["red"])
            self.ui(messagebox.showerror, "Error crítico", f"Fallo durante la transmisión:\n{msg}")
        finally:
            if s:
                s.close()
            self.ui(self.btn_enviar.set_enabled, True)


if __name__ == "__main__":
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Texto nítido en Windows
    except Exception:
        pass
    root = tk.Tk()
    TransferApp(root)
    root.mainloop()
