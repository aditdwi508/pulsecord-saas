Buat virtual environment dan install seluruh library:
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt

Buat file .env berdasarkan .env.example, lalu isi kunci enkripsi Fernet yang digenerate lewat terminal:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Jalankan aplikasi menggunakan 2 terminal terpisah:
Terminal 1 (Web Dashboard):
uvicorn main:app --reload --port 8000
Terminal 2 (Background Worker):
python worker.py
