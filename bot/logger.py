"""Logger equivalente a WriteLog del MQL5: consola + CSV.

CSV: time, type, message, price, lots, balance  (separador coma).
Archivo: <log_file_name>_<symbol>.csv (modo append).
"""

import csv
import datetime
import os


class Logger:
    def __init__(self, enable_file=True, file_name="Gold_HyperGrinder_v20", symbol="XAUUSD"):
        self.enable_file = enable_file
        self.path = None
        if enable_file:
            safe_symbol = symbol.replace("/", "_")
            self.path = f"{file_name}_{safe_symbol}.csv"
            if not os.path.exists(self.path):
                with open(self.path, "w", newline="", encoding="ansi", errors="replace") as f:
                    csv.writer(f).writerow(
                        ["time", "type", "message", "price", "lots", "balance"]
                    )

    def write(self, log_type, message, price=0.0, lots=0.0, balance=0.0):
        ts = datetime.datetime.now().strftime("%Y.%m.%d %H:%M:%S")
        print(f"[{log_type}] {message} | Price: {price:.2f} | Vol: {lots:.2f} | Bal: {balance:.2f}")
        if self.enable_file and self.path:
            try:
                with open(self.path, "a", newline="", encoding="ansi", errors="replace") as f:
                    csv.writer(f).writerow(
                        [ts, log_type, message, f"{price:.2f}", f"{lots:.2f}", f"{balance:.2f}"]
                    )
            except OSError as e:
                print(f"[LOG-ERROR] No se pudo escribir CSV: {e}")
