import os
import socket
import struct
import threading
import tkinter as tk
from tkinter import filedialog, messagebox
import zipfile
import pathlib

PORT = 50008  # Puerto de red para la transferencia

class TransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Mainbit DataLink - Transferencia P2P")
        self.root.geometry("480x520")
        self.root.config(bg="#0f172a")
        self.root.resizable(False, False)
        
        # Efecto de transparencia moderna (95% de opacidad)
        try:
            self.root.attributes('-alpha', 0.95)
        except Exception:
            pass

        self.crear_interfaz()

    def crear_interfaz(self):
        # Título
        titulo = tk.Label(
            self.root, text="Asistente de Migración P2P",
            font=("Segoe UI", 14, "bold"), bg="#0f172a", fg="#f8fafc"
        )
        titulo.pack(pady=20)

        # Marco Informativo / Estado
        self.lbl_estado = tk.Label(
            self.root, text="Selecciona el rol para esta computadora",
            font=("Segoe UI", 10), bg="#1e293b", fg="#94a3b8", padx=15, pady=10
        )
        self.lbl_estado.pack(fill="x", padx=25, pady=5)

        # SECCIÓN 1: PC NUEVA (RECEPTOR)
        frame_rec = tk.LabelFrame(
            self.root, text=" PC Nueva (Recibir e integrar en BACKUP) ",
            font=("Segoe UI", 10, "bold"), bg="#0f172a", fg="#38bdf8", padx=15, pady=12
        )
        frame_rec.pack(fill="x", padx=25, pady=10)

        self.btn_servidor = tk.Button(
            frame_rec, text="Modo Receptor (Esperar Archivos)",
            bg="#0284c7", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.iniciar_servidor_hilo
        )
        self.btn_servidor.pack(fill="x", pady=5)

        # SECCIÓN 2: PC VIEJA (EMISOR)
        frame_env = tk.LabelFrame(
            self.root, text=" PC Vieja (Enviar datos por Ethernet) ",
            font=("Segoe UI", 10, "bold"), bg="#0f172a", fg="#4ade80", padx=15, pady=12
        )
        frame_env.pack(fill="x", padx=25, pady=10)

        lbl_ip = tk.Label(frame_env, text="IP de la PC Nueva:", bg="#0f172a", fg="#cbd5e1", font=("Segoe UI", 9), anchor="w")
        lbl_ip.pack(fill="x", pady=2)

        # CORREGIDO: Se usan ipadx e ipady correctamente
        self.entry_ip = tk.Entry(frame_env, font=("Segoe UI", 10), bg="#334155", fg="white", insertbackground="white", relief="flat")
        self.entry_ip.pack(fill="x", pady=5, ipadx=4, ipady=2)

        self.btn_enviar = tk.Button(
            frame_env, text="Seleccionar Carpetas y Enviar",
            bg="#16a34a", fg="white", font=("Segoe UI", 9, "bold"),
            relief="flat", cursor="hand2", command=self.enviar_datos
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

    # --- LÓGICA DEL RECEPTOR (PC NUEVA) ---
    def iniciar_servidor_hilo(self):
        ip_actual = self.obtener_ip_local()
        self.lbl_estado.config(
            text=f"Escuchando en IP: {ip_actual}\nConecta el cable Ethernet y espera datos...",
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
        server.listen(1)

        try:
            while True:
                conn, addr = server.accept()
                self.root.after(0, lambda: self.lbl_estado.config(text=f"Conectado con: {addr[0]}\nRecibiendo archivos...", fg="#facc15"))
                
                data_size = conn.recv(8)
                filesize = struct.unpack("!Q", data_size)[0]

                zip_path = os.path.join(backup_dir, "datos_temporales.zip")
                received = 0

                with open(zip_path, "wb") as f:
                    while received < filesize:
                        chunk = conn.recv(1024 * 64)
                        if not chunk:
                            break
                        f.write(chunk)
                        received += len(chunk)

                conn.close()

                self.root.after(0, lambda: self.lbl_estado.config(text="Descomprimiendo archivos en 'BACKUP_MIGRACION'...", fg="#facc15"))
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    zip_ref.extractall(backup_dir)
                os.remove(zip_path)

                self.root.after(0, lambda: messagebox.showinfo("¡Éxito!", f"¡Migración completada con éxito!\nLos datos están en el Escritorio:\n{backup_dir}"))
                self.root.after(0, lambda: self.lbl_estado.config(text="¡Transferencia finalizada con éxito!", fg="#4ade80"))
                break
        except Exception as e:
            self.root.after(0, lambda: messagebox.showerror("Error", f"Error en la recepción: {str(e)}"))

    # --- LÓGICA DEL EMISOR (PC VIEJA) ---
    def enviar_datos(self):
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

        self.lbl_estado.config(text="Comprimiendo carpetas seleccionadas...", fg="#facc15")
        self.root.update()

        zip_filename = "migracion_temp.zip"
        try:
            with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for carpeta in carpetas_a_enviar:
                    for root_dir, dirs, files in os.walk(carpeta):
                        for file in files:
                            full_path = os.path.join(root_dir, file)
                            relative_path = os.path.relpath(full_path, os.path.join(carpeta, '..'))
                            zipf.write(full_path, relative_path)

            self.lbl_estado.config(text=f"Enviando datos a {ip_destino}...", fg="#facc15")
            self.root.update()

            filesize = os.path.getsize(zip_filename)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.connect((ip_destino, PORT))

            s.sendall(struct.pack("!Q", filesize))

            with open(zip_filename, "rb") as f:
                while True:
                    chunk = f.read(1024 * 64)
                    if not chunk:
                        break
                    s.sendall(chunk)

            s.close()
            os.remove(zip_filename)

            messagebox.showinfo("Éxito", "¡Archivos enviados y transferidos correctamente a la PC nueva!")
            self.lbl_estado.config(text="¡Envío completado con éxito!", fg="#4ade80")

        except Exception as e:
            if os.path.exists(zip_filename):
                os.remove(zip_filename)
            messagebox.showerror("Error de Envío", f"No se pudo conectar o enviar los datos:\n{str(e)}")
            self.lbl_estado.config(text="Error en la transferencia", fg="#fef2f2")

if __name__ == "__main__":
    root = tk.Tk()
    app = TransferApp(root)
    root.mainloop()
