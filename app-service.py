import threading
import time
import serial
import socketserver
import json
from queue import Queue, Empty
import re
import sys
import os
from resource_path import resource_path
from save_indicacion import save_indicacion
from datetime import datetime 
# --- 1. CONFIGURACIÓN DEL INSTRUMENTO SERIAL (RPM4) ---
# 🛑 ¡AJUSTA ESTA VARIABLE AL PUERTO SERIAL REAL DE TU INSTRUMENTO! 🛑
SERIAL_PORT = input("Introduce el puerto serial del instrumento (ej. COM3 o /dev/ttyUSB0): ").strip()
BAUD_RATE = int(input("Introduce la velocidad de baudios (ej. 9600): ").strip())
PARITY = input("Introduce la paridad (N=None, E=Even, O=Odd): ").strip().upper()
BYTESIZE = int(input("Introduce el número de bits de datos (ej. 8): ").strip())
STOPBIST = int(input("Introduce el número de bits de parada (ej. 1): ").strip())
# Timeout crucial: Aumentado a 2.0 segundos para dar tiempo al instrumento a responder
# Esto evita que la función se "cuelgue" indefinidamente si no hay respuesta.
TIMEOUT = float(input("Introduce el timeout en segundos para la comunicación serial (ej. 2.0): ").strip())

# Comando típico para lectura en RPM4 (PR = Pressure Read, terminado en CR).
# He cambiado "SEND" por "PR", que es más estándar para RPM4. Ajusta si es necesario.
READ_COMMAND_STR = "#0100CH*" 
READ_COMMAND_BYTES = (READ_COMMAND_STR + '\r').encode('ascii')

# --- 2. CONFIGURACIÓN DEL SERVIDOR TCP/IP ---
TCP_HOST = "0.0.0.0"  # Escucha en todas las interfaces de red
TCP_PORT = 5000  # Puerto de red para que los clientes se conecten

# --- 3. VARIABLES GLOBALES Y MECANISMOS DE SINCRONIZACIÓN ---
# La lectura inicial es un diccionario para mantener la consistencia con el JSON
LAST_READING = {"status": "SERVICIO_INICIADO"} 
data_lock = threading.Lock() 
stop_event = threading.Event()
command_queue = Queue() 

def obtener_ruta_guardado(nombre_archivo):
    """
    Calcula la ruta en la carpeta real donde se encuentra el archivo ejecutable (.exe)
    o el script (.py), evitando las carpetas temporales de PyInstaller.
    """
    if getattr(sys, 'frozen', False):
        # Si corre desde el .exe, guarda al lado del .exe
        base_path = os.path.dirname(sys.executable)
    else:
        # Si corre en modo desarrollo .py
        base_path = os.path.dirname(os.path.abspath(__file__))
    
    return os.path.join(base_path, nombre_archivo)

# ----------------------------------------------------------------------
#                         HILO LECTOR SERIAL (THE CONSUMER)
# ----------------------------------------------------------------------

def serial_reader_thread(port, baud, parity, bytesize):
    """
    Bucle principal con lógica de reconexión. Intenta mantener la conexión 
        serial viva o reestablecerla en caso de fallo (SerialException).
    """
    while not stop_event.is_set():
        ser = None
        try:
            print(f"[SERIAL] ⏳ Intentando conectar a {port}...")
            
            # --- CONEXIÓN INICIAL ---
            ser = serial.Serial(
                port=port,
                baudrate=baud,
                parity=parity,
                stopbits=STOPBIST,
                bytesize=bytesize,
                timeout=TIMEOUT,
                write_timeout=TIMEOUT # Añadido timeout de escritura para mayor robustez
            )

            # Espera forzada para reinicio del instrumento (CRÍTICO para RPM4/Arduino)
            time.sleep(2.5) 
            
            if not ser.is_open:
                raise serial.SerialException("No se pudo abrir el puerto al primer intento.")
            
            print(f"[SERIAL] 🟢 Conexión serial establecida en {port}.")
            
            # --- BUCLE DE LECTURA CONTINUA ---
            read_loop(ser) # La lógica de lectura se mueve a una función interna
            
        except serial.SerialException as e:
            # Esta excepción ocurre si el puerto se desconecta o no se puede abrir
            print(f"[SERIAL] ❌ Error de hardware/conexión: {e}. Reintentando en 5s...")
            # Actualizar estado global para informar a los clientes del error
            with data_lock:
                LAST_READING = {"status": "ERROR_SERIAL", "error": str(e)}
            time.sleep(5) # Esperar antes de reintentar la conexión
            
        except Exception as e:
            # Captura de cualquier otro error inesperado
            print(f"[SERIAL] ❌ Error inesperado en el hilo lector: {e}. Reintentando en 5s...")
            with data_lock:
                LAST_READING = {"status": "ERROR_INESPERADO", "error": str(e)}
            time.sleep(5)
            
        finally:
            # Asegura que el puerto se cierre si se abrió correctamente
            if ser and ser.is_open:
                ser.close()
                print("[SERIAL] 🔌 Puerto cerrado.")

    print("[SERIAL] 🛑 Hilo Lector finalizado por stop_event.")


def read_loop(ser):
    """Bucle interno de lectura y procesamiento de datos."""
    global LAST_READING

    while not stop_event.is_set():
        try:
            # 1. Enviar comando (petición de lectura)
            ser.write(READ_COMMAND_BYTES) 

            # 2. Leer la respuesta
            # Usamos b'\r' como terminador, común en instrumentos RPM4.
            # ser.read_until() espera hasta que el terminador o el timeout ocurran.
            response_bytes = ser.readline().decode("ascii",errors='ignore').strip()
            # response_bytes = "#0001CH1=1747CH2=2332CH3=758CH4=1834CH5=0CH6=0CH7=0CH8=0*"
            
            if response_bytes:
                match1 = re.search(r'CH1=(\d+)', response_bytes)
                match2 = re.search(r'CH2=(\d+)', response_bytes)
                match3 = re.search(r'CH3=(\d+)', response_bytes)
                match4 = re.search(r'CH4=(\d+)', response_bytes)
                print(f"Match1: {match1.group(1) if match1 else 'No encontrado'}, Match2: {match2.group(1) if match2 else 'No encontrado'}, Match3: {match3.group(1) if match3 else 'No encontrado'}, Match4: {match4.group(1) if match4 else 'No encontrado'}")
                if match1:
                    valor_temp = float(match1.group(1))
                    valor_hr = float(match2.group(1))
                    valor_presion = float(match3.group(1))
                    # Guardar en CSV
                    valor_temp_2 = float(match4.group(1))
                    
                    # AJUSTAR INDICACION DE PRESION
                    #Presion = maximo-((( 4095-Dato) * (max - Minimo)) / 4095) + Offset
                    valor_presion = (((4095-valor_presion)*(1050-750))/4095) # Invertir el valor para que 0 sea 4095 y viceversa
                    # valor_presion = (4095-int(valor_presion)) * (1050 - 750) / 4095  # Invertir el valor para que 0 sea 4095 y viceversa
                    valor_presion = 1050 - valor_presion  # Ajustar para que 0 corresponda a 1050 y 4095 a 750
                    
                    
                    valor_temp = 41.70-(((4095-valor_temp)*(41.70-0)))/4095 # Invertir el valor para que 0 sea 4095 y viceversa
                    valor_temp = round(valor_temp, 2)  # Redondear a 2 decimales
                    
                    valor_temp_2 = 41.70-(((4095-valor_temp_2)*(41.70-0))/4095) + (-0.80) # Invertir el valor para que 0 sea 4095 y viceversa
                    valor_temp_2 = round(valor_temp_2, 2)  # Redondear a 2 decimales
                    
                    
                    #Ajustar Indicacion De Humedad Relativa
                    valor_hr = 100-(((3194-valor_hr)*(100-0)))/2539
                    
                    nombre_csv = f"Condiciones Ambientales_{datetime.now().strftime('%Y-%m-%d')}.csv"
                    csv_path = obtener_ruta_guardado(nombre_csv)
                    
                    try:
                        res = save_indicacion(
                            0, valor_presion, valor_temp, valor_hr, valor_temp_2,
                            csv_path,
                        )
                    except Exception as e:
                        import traceback
                        print(f"[WARN] Error guardando indicaciones en CSV: {e}")
                        traceback.print_exc()
                    
                    reading = {
                    "type": "reading",
                    "press": valor_presion or 0,  # Convertir a kPa y ajustar
                    "pressUnidad": "hPa",  # Convertir a kPa y ajustar
                    "temperature_c": valor_temp or 0,
                    "temp_1_unidad": "C",
                    "temperature_c_2": valor_temp_2 or 0,  # Evitar valores negativos o nulos
                    "temp_2_unidad": "C",
                    "humidity_rh": valor_hr or 0,  # Evitar valores negativos o nulos
                    "humidity_unidad": " HR",
                    "raw": response_bytes,
                    'unidad': "hPa"
                }
                    
                    # 3. Almacenar la lectura de forma segura
                    with data_lock:
                        LAST_READING = reading
                    
                    print(f"[SERIAL] ✅ Lectura: {reading['press']} {reading['unidad']}, {reading['temperature_c']} °C, {reading['humidity_rh']}% HR, {reading['temperature_c_2']} °C")
                else:
                    print(f"[SERIAL] ⚠️ Dato sin formato esperado: {response_bytes}")

            else:
                # Timeout, no se recibieron datos después del comando
                print(f"[SERIAL] ⏳ Timeout ({TIMEOUT}s) - No hay respuesta después de enviar {READ_COMMAND_STR}.")
            
            # Pausa entre ciclos de lectura
            time.sleep(1.0) 

        except serial.SerialTimeoutException:
            # Si hay un error de timeout aquí, es mejor relanzarlo para forzar la reconexión
            print("[SERIAL] ❌ Serial Timeout. La comunicación se perdió.")
            raise # Esto hace que el bucle principal intente reconectar
            
        except serial.SerialException as e:
            # Si el puerto se desconecta, también se relanza para reconectar
            print(f"[SERIAL] ❌ Serial Error. Puerto desconectado: {e}")
            raise 

# ----------------------------------------------------------------------
#                       HILO DISTRIBUIDOR TCP/IP (THE PROXY)
# ----------------------------------------------------------------------
class TCPDataHandler(socketserver.BaseRequestHandler):
    def handle(self):
        print(f"[TCP] 🔗 Nuevo cliente conectado desde: {self.client_address[0]}")
        try:
            self.data = self.request.recv(1024).strip()
            if self.data == b'GET_READING':
                with data_lock:
                    reading_data = LAST_READING
                response_str = json.dumps(reading_data)
                response_bytes = (response_str + '\r\n').encode('ascii')
                self.request.sendall(response_bytes)
                print(f"[TCP] Enviado: {response_str[:40]}... a {self.client_address[0]}")
            else:
                        self.request.sendall(b"Error: Comando desconocido. Usa 'GET_READING'\r\n")
        except Exception as e:
            print(f"[TCP] ❌ Error manejando cliente {self.client_address[0]}: {e}")
        finally:
          pass # El socketserver cierra la conexión automáticamente

def run_service():
    """Ejecuta el lector serial y el servidor TCP simultáneamente."""

    # 1. Iniciar el Hilo Lector Serial
    reader_thread = threading.Thread(
    target=serial_reader_thread, 
    args=(SERIAL_PORT, BAUD_RATE, PARITY, BYTESIZE),
    daemon=True
    )
    reader_thread.start()
 
    time.sleep(3) # Tiempo para que el hilo lector intente conectarse
 
 # 2. Iniciar el Servidor TCP/IP (Hilo Distribuidor)
    try:
        server = socketserver.ThreadingTCPServer((TCP_HOST, TCP_PORT), TCPDataHandler)
        print(f"\n[TCP] 🟢 Servidor TCP iniciado. Escuchando en {TCP_HOST}:{TCP_PORT}")
        print("[MAIN] Presiona Ctrl+C para detener el servicio de forma segura.\n")
 
        server.serve_forever()

    except KeyboardInterrupt:
        print("\n[MAIN] 🛑 Señal de interrupción recibida.")
    except Exception as e:
        print(f"[MAIN] ❌ Error al iniciar el servidor TCP: {e}")
 
    finally:
        # 3. Parada segura y liberación de recursos
        print("[MAIN] ⏳ Deteniendo servicios...")
        stop_event.set() # Activar evento de parada para el hilo serial
        if 'server' in locals() and server:
            server.shutdown() # Detiene el servidor TCP
 
        reader_thread.join(timeout=5)
        print("[MAIN] Servicios detenidos. ¡Adiós!")

if __name__ == "__main__":
    run_service()