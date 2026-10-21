import os
import socket
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

PORT = 50008  # Puerto de red para la transferencia

class TransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mainbit DataLink Pro - Transferencia P2P")
        self.root.geometry("480x520")
        self.root.config(bg="#0f172a")
        self.root.resizable(False, False)
        
        try:
            self.root.attributes('-alpha', 0.95)
        except Exception:
            pass

        self.crear_interfaz()

    def crear_interfaz(self):
        titulo = tk.Label(
            self.root, text="Asistente de Migración P2P (Ultra Rápido)",
            font=("Segoe UI", 13, "bold"), bg="#0f172a", fg="#f8fafc"
        )
        titulo.pack(pady=20)

        self.lbl_estado = tk.Label(
            self.root, text="Selecciona el rol para esta computadora",
            font=("Segoe UI", 10), bg="#1e293b", fg="#94a3b8", padx=15, pady=10
        )
        self.lbl_estado.pack(fill="x", padx=25, pady=5)

        # SECCIÓN 1: PC NUEVA (RECEPTOR)
        frame_rec = tk.LabelFrame(
            self.root, text=" PC Nueva (Recibir archivos en BACKUP) ",
            font=("Segoe UI", 10, "bold"), bg="#0f172a", fg="#38bdf8", padx=15, pady=12
        )
        frame_rec.pack(fill="x", padx=25, pady=10)

        self.btn_servidor = tk.Button(
            frame_rec, text="Modo Receptor (Esperar Conexión)",
            bg="#0284c7", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.iniciar_servidor_hilo
        )
        self.btn_servidor.pack(fill="x", pady=5)

        # SECCIÓN 2: PC VIEJA (EMISOR)
        frame_env = tk.LabelFrame(
            self.root, text=" PC Vieja (Enviar por Ethernet) ",
            font=("Segoe UI", 10, "bold"), bg="#0f172a", fg="#4ade80", padx=15, pady=12
        )
        frame_env.pack(fill="x", padx=25, pady=10)

        lbl_ip = tk.Label(frame_env, text="IP de la PC Nueva:", bg="#0f172a", fg="#cbd5e1", font=("Segoe UI", 9), anchor="w")
        lbl_ip.pack(fill="x", pady=2)

        self.entry_ip = tk.Entry(frame_env, font=("Segoe UI", 10), bg="#334155", fg="white", insertbackground="white", relief="flat")
        self.entry_ip.pack(fill="x", pady=5, ipadx=4, ipady=2)

        self.btn_enviar = tk.Button(
            frame_env, text="Seleccionar Carpetas y Enviar",
            bg="#16a34a", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.iniciar_envio_hilo
        )
        self.btn_enviar.pack(fill="x", pady=5)

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
        self.lbl_estado.config(
            text=f"Escuchando en IP: {ip_actual}\nEsperando conexión por Ethernet...",
            fg="#38bdf8"
        )
        self.btn_servidor.config(state="disabled", bg="#64748b")
        threading.Thread(target=self.ejecutar_servidor, daemon=True).start()

    def ejecutar_servidor(self):
        desktop = os.path.join(os.path.expanduser("~"), "Desktop")
        backup_dir = os.path.join(desktop, "BACKUP_MIGRACION")
        os.makedirs(backup_dir, exist_ok=True)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", PORT))
        server.listen(5)

        try:
            conn, addr = server.accept()
            self.root.after(0, lambda: self.lbl_estado.config(text=f"Conectado con PC vieja: {addr[0]}\nRecibiendo archivos...", fg="#facc15"))

            while True:
                # Leer tamaño de la ruta del archivo (4 bytes)
                raw_path_len = conn.recv(4)
                if not raw_path_len:
                    break
                path_len = struct.unpack("!I", raw_path_len)[0]

                # Si path_len es 0, significa fin de la transmisión
                if path_len == 0:
                    break

                # Leer ruta relativa
                rel_path = conn.recv(path_len).decode('utf-8')
                
                # Leer tamaño del archivo (8 bytes)
                raw_file_size = conn.recv(8)
                file_size = struct.unpack("!Q", raw_file_size)[0]

                # Crear ruta de destino completa
                dest_path = os.path.join(backup_dir, rel_path)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)

                # Recibir contenido del archivo por bloques
                received = 0
                with open(dest_path, "wb") as f:
                    while received < file_size:
                        chunk_size = min(1024 * 64, file_size - received)
                        chunk = conn.recv(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)

            conn.close()
            server.close()

            self.root.after(0, lambda: messagebox.showinfo("¡Éxito!", f"¡Migración completada con éxito!\nLos archivos están en:\n{backup_dir}"))
            self.root.after(0, lambda: self.lbl_estado.config(text="¡Transferencia finalizada con éxito!", fg="#4ade80"))
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Error en la recepción: {str(e)}"))

    # --- EMISOR (PC VIEJA) ---
    def iniciar_envio_hilo(self):
        ip_destino = self.entry_ip.get().strip()
        if not ip_destino:
            messagebox.showerror("Error", "Por favor ingresa la dirección IP de la PC nueva.")
            return

        carpetas_a_enviar = []
        while True:
            dir_sel = filedialog.askdirectory(title="Selecciona una carpeta para respaldar (Cancela para terminar)")
            if not dir_sel:
                break
            carpetas_a_enviar.append(dir_sel)

        if not carpetas_a_enviar:
            return

        self.btn_enviar.config(state="disabled", bg="#64748b")
        self.lbl_estado.config(text="Iniciando transmisión directa...", fg="#facc15")
        
        threading.Thread(target=self.ejecutar_envio, args=(ip_destino, carpetas_a_enviar), daemon=True).start()

    def ejecutar_envio(self, ip_destino, carpetas):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip_destino, PORT))

            total_archivos = 0
            for carpeta in carpetas:
                base_name = os.path.basename(carpeta)
                for root_dir, dirs, files in os.walk(carpeta):
                    for file in files:
                        total_archivos += 1

            enviados = 0
            for carpeta in carpeta_s if 'carpeta_s' in locals() else carpetas:
                base_name = os.path.basename(carpeta)
                for root_dir, dirs, files in os.walk(carpeta):
                    for file in files:
                        full_path = os.path.join(root_dir, file)
                        # Calcular ruta relativa para mantener la estructura dentro de BACKUP
                        rel_dir = os.path.relpath(root_dir, os.path.dirname(carpeta))
                        rel_path = os.path.join(rel_dir, file)

                        path_bytes = rel_path.encode('utf-8')
                        file_size = os.path.getsize(full_path)

                        # Enviar longitud de la ruta y ruta
                        s.sendall(struct.pack("!I", len(path_bytes)))
                        s.sendall(path_bytes)

                        # Enviar tamaño del archivo y contenido
                        s.sendall(struct.pack("!Q", file_size))

                        with open(full_path, "rb") as f:
                            while True:
                                chunk = f.read(1024 * 64)
                                if not chunk:
                                    break
                                s.sendall(chunk)

                        enviados += 1
                        self.root.after(0, lambda e=enviados, t=total_archivos: self.lbl_estado.config(
                            text=f"Enviando archivo {e} de {t}...", fg="#facc15"
                        ))

            # Señal de fin de transmisión
            s.sendall(struct.pack("!I", 0))
            s.close()

            self.root.after(0, lambda: messagebox.showinfo("Éxito", "¡Archivos transferidos directamente a la PC nueva con éxito!"))
            self.root.after(0, lambda: self.lbl_estado.config(text="¡Envío completado!", fg="#4ade80"))
            self.root.after(0, lambda: self.btn_enviar.config(state="normal", bg="#16a34a"))

        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error de Envío", f"Fallo en la conexión o envío:\n{str(e)}"))
            self.root.after(0, lambda: self.lbl_estado.config(text="Error en la transferencia", fg="#fef2f2"))
            self.root.after(0, lambda: self.btn_enviar.config(state="normal", bg="#16a34a"))

if __name__ == "__main__":
    root = tk.Tk()
    app = TransferApp(root)
    root.mainloop()
