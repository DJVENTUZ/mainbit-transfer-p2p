import os
import socket
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, Listbox, Scrollbar

PORT_TRANSFER = 50008
PORT_DISCOVER = 50009

class TransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mainbit DataLink // iOS Edition")
        self.root.geometry("500x680")
        self.root.config(bg="#090d16")
        self.root.resizable(False, False)
        
        # Efecto de transparencia translúcida estilo iOS (96%)
        try:
            self.root.attributes('-alpha', 0.96)
        except Exception:
            pass

        self.rutas_seleccionadas = []
        self.crear_estilos()
        self.crear_interfaz()

    def crear_estilos(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor='#1e293b',
            background='#38bdf8',
            darkcolor='#0284c7',
            lightcolor='#38bdf8',
            bordercolor='#1e293b',
            thickness=8
        )

    def crear_interfaz(self):
        # --- HEADER / LOGO MAINBIT ---
        header_frame = tk.Frame(self.root, bg="#090d16")
        header_frame.pack(fill="x", padx=20, pady=(15, 5))

        lbl_logo = tk.Label(
            header_frame, text="M A I N B I T",
            font=("Segoe UI", 16, "bold"), bg="#090d16", fg="#38bdf8"
        )
        lbl_logo.pack(side="left")

        lbl_badge = tk.Label(
            header_frame, text="DATALINK PRO",
            font=("Segoe UI", 8, "bold"), bg="#1e293b", fg="#4ade80", padx=8, pady=3
        )
        lbl_badge.pack(side="right")

        # --- ESTADO / INFO ---
        self.lbl_estado = tk.Label(
            self.root, text="Selecciona el rol para comenzar la migración",
            font=("Segoe UI", 9), bg="#131c2e", fg="#94a3b8", padx=15, pady=8, relief="flat"
        )
        self.lbl_estado.pack(fill="x", padx=20, pady=5)

        # --- SECCIÓN 1: PC NUEVA (RECEPTOR) ---
        frame_rec = tk.LabelFrame(
            self.root, text=" PC Nueva (Receptor) ",
            font=("Segoe UI", 9, "bold"), bg="#090d16", fg="#38bdf8", padx=12, pady=6
        )
        frame_rec.pack(fill="x", padx=20, pady=5)

        self.btn_servidor = tk.Button(
            frame_rec, text="Activar Modo Receptor (Esperar Datos)",
            bg="#0284c7", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.iniciar_servidor_hilo
        )
        self.btn_servidor.pack(fill="x", pady=3, ipady=3)

        # --- SECCIÓN 2: PC VIEJA (EMISOR) ---
        frame_env = tk.LabelFrame(
            self.root, text=" PC Vieja (Emisor y Selección) ",
            font=("Segoe UI", 9, "bold"), bg="#090d16", fg="#4ade80", padx=12, pady=8
        )
        frame_env.pack(fill="x", padx=20, pady=5)

        lbl_ip = tk.Label(frame_env, text="IP de la PC Nueva:", bg="#090d16", fg="#cbd5e1", font=("Segoe UI", 8), anchor="w")
        lbl_ip.pack(fill="x", pady=1)

        frame_ip_row = tk.Frame(frame_env, bg="#090d16")
        frame_ip_row.pack(fill="x", pady=2)

        # CORREGIDO: Se usa ipadx e ipady en lugar de ipx e ipy
        self.entry_ip = tk.Entry(frame_ip_row, font=("Segoe UI", 9), bg="#1e293b", fg="white", insertbackground="white", relief="flat")
        self.entry_ip.pack(side="left", fill="x", expand=True, ipadx=3, ipady=4, padx=(0, 5))

        self.btn_buscar = tk.Button(
            frame_ip_row, text="🔍 Auto-Buscar",
            bg="#334155", fg="#f8fafc", font=("Segoe UI", 8, "bold"),
            relief="flat", cursor="hand2", command=self.buscar_pc_nueva
        )
        self.btn_buscar.pack(side="right", ipy=4, ipadx=6)

        # Botones de selección de archivos y carpetas
        frame_botones_sel = tk.Frame(frame_env, bg="#090d16")
        frame_botones_sel.pack(fill="x", pady=5)

        btn_add_carp = tk.Button(
            frame_botones_sel, text="+ Carpeta", bg="#131c2e", fg="#38bdf8",
            font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", command=self.agregar_carpeta
        )
        btn_add_carp.pack(side="left", expand=True, fill="x", padx=(0, 2), ipy=4)

        btn_add_arch = tk.Button(
            frame_botones_sel, text="+ Archivo(s)", bg="#131c2e", fg="#38bdf8",
            font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", command=self.agregar_archivos
        )
        btn_add_arch.pack(side="left", expand=True, fill="x", padx=(2, 2), ipy=4)

        btn_del_item = tk.Button(
            frame_botones_sel, text="🗑 Quitar", bg="#131c2e", fg="#f87171",
            font=("Segoe UI", 8, "bold"), relief="flat", cursor="hand2", command=self.quitar_item
        )
        btn_del_item.pack(side="right", expand=True, fill="x", padx=(2, 0), ipy=4)

        # Lista visual de elementos seleccionados
        frame_lista = tk.Frame(frame_env, bg="#090d16")
        frame_lista.pack(fill="x", pady=3)

        self.listbox = Listbox(
            frame_lista, height=4, bg="#131c2e", fg="#f8fafc", font=("Segoe UI", 8),
            selectbackground="#0284c7", relief="flat", highlightthickness=0
        )
        self.listbox.pack(side="left", fill="both", expand=True)

        scrollbar = Scrollbar(frame_lista, orient="vertical", command=self.listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.listbox.config(yscrollcommand=scrollbar.set)

        self.btn_enviar = tk.Button(
            frame_env, text="Iniciar Transferencia Ultrarrápida",
            bg="#16a34a", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.iniciar_envio_hilo
        )
        self.btn_enviar.pack(fill="x", pady=6, ipady=3)

        # --- BARRA DE PROGRESO ---
        self.progress = ttk.Progressbar(
            self.root, orient="horizontal", length=440, mode="determinate", style="Custom.Horizontal.TProgressbar"
        )
        self.progress.pack(pady=8)

        # --- FOOTER / FIRMA ---
        footer_frame = tk.Frame(self.root, bg="#090d16")
        footer_frame.pack(fill="x", padx=20, pady=(0, 10))

        lbl_firma = tk.Label(
            footer_frame, text="Desarrollado para Mainbit por Gabriel Hernández Arroyo",
            font=("Segoe UI", 7), bg="#090d16", fg="#64748b"
        )
        lbl_firma.pack(side="center")

    def agregar_carpeta(self):
        dir_sel = filedialog.askdirectory(title="Selecciona carpeta")
        if dir_sel and dir_sel not in self.rutas_seleccionadas:
            self.rutas_seleccionadas.append(dir_sel)
            self.listbox.insert(tk.END, f"[Carpeta] {dir_sel}")

    def agregar_archivos(self):
        files_sel = filedialog.askopenfilenames(title="Selecciona archivos")
        for f in files_sel:
            if f and f not in self.rutas_seleccionadas:
                self.rutas_seleccionadas.append(f)
                self.listbox.insert(tk.END, f"[Archivo] {f}")

    def quitar_item(self):
        seleccion = self.listbox.curselection()
        if seleccion:
            index = seleccion[0]
            self.listbox.delete(index)
            del self.rutas_seleccionadas[index]

    def obtener_ip_local(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    # --- RECEPTOR (PC NUEVA) ---
    def iniciar_servidor_hilo(self):
        ip_actual = self.obtener_ip_local()
        self.lbl_estado.config(text=f"Escuchando en IP: {ip_actual} (Esperando datos...)", fg="#38bdf8")
        self.btn_servidor.config(state="disabled", bg="#334155")
        threading.Thread(target=self.ejecutar_servidor_archivos, daemon=True).start()
        threading.Thread(target=self.ejecutar_servidor_discovery, daemon=True).start()

    def ejecutar_servidor_discovery(self):
        disc_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        disc_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            disc_sock.bind(('', PORT_DISCOVER))
            while True:
                data, addr = disc_sock.recvfrom(1024)
                if data == b"MAINBIT_DISCOVER":
                    disc_sock.sendto(b"MAINBIT_HERE", addr)
        except Exception:
            pass

    def ejecutar_servidor_archivos(self):
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        backup_dir = os.path.join(desktop, "BACKUP_MIGRACION")
        os.makedirs(backup_dir, exist_ok=True)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", PORT_TRANSFER))
        server.listen(5)

        try:
            while True:
                conn, addr = server.accept()
                
                raw_total_bytes = conn.recv(8)
                if not raw_total_bytes:
                    conn.close()
                    break
                total_bytes = struct.unpack("!Q", raw_total_bytes)[0]
                
                received_bytes = 0
                self.root.after(0, lambda: self.lbl_estado.config(text=f"Conectado con {addr[0]}. Recibiendo datos...", fg="#facc15"))

                while True:
                    raw_path_len = conn.recv(4)
                    if not raw_path_len:
                        break
                    path_len = struct.unpack("!I", raw_path_len)[0]
                    if path_len == 0:
                        break

                    rel_path = conn.recv(path_len).decode('utf-8')
                    raw_file_size = conn.recv(8)
                    file_size = struct.unpack("!Q", raw_file_size)[0]

                    dest_path = os.path.join(backup_dir, rel_path)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

                    file_received = 0
                    with open(dest_path, "wb") as f:
                        while file_received < file_size:
                            chunk_size = min(1024 * 128, file_size - file_received)
                            chunk = conn.recv(chunk_size)
                            if not chunk:
                                break
                            f.write(chunk)
                            file_received += len(chunk)
                            received_bytes += len(chunk)
                            
                            pct = int((received_bytes / total_bytes) * 100) if total_bytes > 0 else 0
                            self.root.after(0, lambda p=pct: self.progress.config(value=p))

                conn.close()
                self.root.after(0, lambda: messagebox.showinfo("Éxito", f"¡Migración completada con éxito!\nGuardado en:\n{backup_dir}"))
                self.root.after(0, lambda: self.lbl_estado.config(text="¡Transferencia finalizada!", fg="#4ade80"))
                break
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Error en recepción: {str(e)}"))

    # --- EMISOR (PC VIEJA) ---
    def buscar_pc_nueva(self):
        self.lbl_estado.config(text="Buscando PC nueva en la red...", fg="#facc15")
        self.root.update()

        def escanear():
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.settimeout(3.0)
                s.sendto(b"MAINBIT_DISCOVER", ('<broadcast>', PORT_DISCOVER))
                data, addr = s.recvfrom(1024)
                if data == b"MAINBIT_HERE":
                    ip_encontrada = addr[0]
                    self.root.after(0, lambda: self.entry_ip.delete(0, tk.END))
                    self.root.after(0, lambda: self.entry_ip.insert(0, ip_encontrada))
                    self.root.after(0, lambda: self.lbl_estado.config(text=f"¡PC Nueva detectada en {ip_encontrada}!", fg="#4ade80"))
                s.close()
            except Exception:
                self.root.after(0, lambda: messagebox.showwarning("Aviso", "No se encontró automáticamente a la PC nueva."))
                self.root.after(0, lambda: self.lbl_estado.config(text="Búsqueda automática agotada", fg="#fef2f2"))

        threading.Thread(target=escanear, daemon=True).start()

    def iniciar_envio_hilo(self):
        ip_destino = self.entry_ip.get().strip()
        if not ip_destino:
            messagebox.showerror("Error", "Ingresa o busca la IP de la PC nueva.")
            return

        if not self.rutas_seleccionadas:
            messagebox.showwarning("Aviso", "No hay archivos o carpetas seleccionados.")
            return

        self.btn_enviar.config(state="disabled", bg="#334155")
        self.progress['value'] = 0
        self.lbl_estado.config(text="Calculando archivos y enviando...", fg="#facc15")
        
        threading.Thread(target=self.ejecutar_envio, args=(ip_destino, self.rutas_seleccionadas), daemon=True).start()

    def ejecutar_envio(self, ip_destino, rutas):
        try:
            archivos_a_enviar = []
            total_bytes = 0

            for ruta in rutas:
                if os.path.isfile(ruta):
                    nombre_archivo = os.path.basename(ruta)
                    tamanio = os.path.getsize(ruta)
                    archivos_a_enviar.append((ruta, nombre_archivo, tamanio))
                    total_bytes += tamanio
                elif os.path.isdir(ruta):
                    for root_dir, dirs, files in os.walk(ruta):
                        for file in files:
                            full_path = os.path.join(root_dir, file)
                            rel_dir = os.path.relpath(root_dir, os.path.dirname(ruta))
                            rel_path = os.path.join(rel_dir, file)
                            tamanio = os.path.getsize(full_path)
                            archivos_a_enviar.append((full_path, rel_path, tamanio))
                            total_bytes += tamanio

            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip_destino, PORT_TRANSFER))

            s.sendall(struct.pack("!Q", total_bytes))

            enviados_bytes = 0
            for full_path, rel_path, file_size in archivos_a_enviar:
                path_bytes = rel_path.encode('utf-8')

                s.sendall(struct.pack("!I", len(path_bytes)))
                s.sendall(path_bytes)
                s.sendall(struct.pack("!Q", file_size))

                with open(full_path, "rb") as f:
                    while True:
                        chunk = f.read(1024 * 128)
                        if not chunk:
                            break
                        s.sendall(chunk)
                        enviados_bytes += len(chunk)

                        pct = int((enviados_bytes / total_bytes) * 100) if total_bytes > 0 else 0
                        self.root.after(0, lambda p=pct: self.progress.config(value=p))

            s.sendall(struct.pack("!I", 0))
            s.close()

            self.root.after(0, lambda: messagebox.showinfo("Éxito", "¡Archivos transferidos exitosamente a la PC nueva!"))
            self.root.after(0, lambda: self.lbl_estado.config(text="¡Envío completado al 100%!", fg="#4ade80"))
            self.root.after(0, lambda: self.btn_enviar.config(state="normal", bg="#16a34a"))

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Error en envío:\n{str(e)}"))
            self.root.after(0, lambda: self.lbl_estado.config(text="Error de transferencia", fg="#fef2f2"))
            self.root.after(0, lambda: self.btn_enviar.config(state="normal", bg="#16a34a"))

if __name__ == "__main__":
    root = tk.Tk()
    app = TransferApp(root)
    root.mainloop()
