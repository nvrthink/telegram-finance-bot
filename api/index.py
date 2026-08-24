import os
import json
import io
import psycopg2
import httpx
from PIL import Image
from fastapi import FastAPI, Request
import google.generativeai as genai

app = FastAPI()

# Configuration from Environment Variables
BOT_TOKEN = os.getenv("8891247016:AAEcUF0VSzpsd-eqz2IjXNYKuvaYxMzQ5JM")
DATABASE_URL = os.getenv("postgresql://neondb_owner:npg_3kdVCz2Fjbth@ep-snowy-hall-aybkcw8s-pooler.c-5.us-east-2.aws.neon.tech/telegram-finance-bot?sslmode=require&channel_binding=require")
GEMINI_API_KEY = os.getenv("AQ.Ab8RN6Iro9KTc1Zh5t3IZZEEGJLgPsdAbPxI5Y906cwjhwIWvg")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Initialize Gemini AI
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)


def get_db():
    """Connect to Neon PostgreSQL"""
    db_url = DATABASE_URL
    if db_url and db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(db_url)


def init_db():
    """Initialize database tables"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                type VARCHAR(10) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                category VARCHAR(50) DEFAULT 'Umum',
                description TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print("Database Init Error:", e)


# Run DB initialization
init_db()


async def send_message(chat_id: int, text: str):
    """Send text response to Telegram user"""
    async with httpx.AsyncClient() as client:
        await client.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        )


async def get_telegram_file_bytes(file_id: str) -> bytes:
    """Download image file from Telegram servers"""
    async with httpx.AsyncClient() as client:
        res = await client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
        file_path = res.json()["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        file_res = await client.get(download_url)
        return file_res.content


def parse_with_gemini_text(user_text: str) -> list:
    """Parse text using Gemini AI into structured transaction JSON"""
    if not GEMINI_API_KEY:
        return []

    prompt = f"""
    Kamu adalah asisten pencatat keuangan profesional.
    Ekstrak data transaksi keuangan dari kalimat berikut ke dalam format JSON array valid.

    Kalimat pengguna: "{user_text}"

    Skema output wajib berupa JSON Array of Objects:
    [
      {{
        "type": "EXPENSE" atau "INCOME",
        "amount": angka murni float/int (misal: 25000),
        "category": "Kategori singkat (Makan, Transport, Belanja, Gaji, Tagihan, Lainnya)",
        "description": "Deskripsi barang/jasa"
      }}
    ]

    Aturan:
    - Konversi otomatis "25rb", "10k", "1.5juta" menjadi nilai numerik murni.
    - Jika kalimat berisi beberapa transaksi, ekstrak semuanya ke dalam list.
    - Kembalikan HANYA format JSON tanpa teks pembuka/penutup atau kode block markdown.
    """

    try:
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(prompt)
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned_text)
    except Exception as e:
        print("Gemini Text Error:", e)
        return []


def parse_with_gemini_vision(image_bytes: bytes) -> list:
    """Parse photo receipt using Gemini Vision OCR"""
    if not GEMINI_API_KEY:
        return []

    prompt = """
    Analisis foto struk/nota belanja ini. Ekstrak total transaksi atau rincian transaksi utama kedalam format JSON array valid.

    Skema output wajib berupa JSON Array of Objects:
    [
      {
        "type": "EXPENSE",
        "amount": angka murni total pembayaran (misal: 85000),
        "category": "Makan/Belanja/Transport/Lainnya",
        "description": "Nama Merchant atau Ringkasan Belanja"
      }
    ]

    Aturan:
    - Ambil total akhir yang dibayarkan.
    - Kembalikan HANYA format JSON murni tanpa markdown/teks tambahan.
    """

    try:
        image = Image.open(io.BytesIO(image_bytes))
        model = genai.GenerativeModel("gemini-1.5-flash")
        response = model.generate_content([prompt, image])
        cleaned_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(cleaned_text)
    except Exception as e:
        print("Gemini Vision Error:", e)
        return []


def save_transactions(user_id: int, transactions: list) -> str:
    """Save extracted transactions to Neon Postgres DB"""
    if not transactions:
        return "❌ Gagal memproses transaksi. Pastikan format teks atau foto jelas."

    conn = get_db()
    cur = conn.cursor()
    saved_summary = []

    for item in transactions:
        t_type = item.get("type", "EXPENSE")
        amount = item.get("amount", 0)
        category = item.get("category", "Umum")
        desc = item.get("description", "-")

        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, category, description) VALUES (%s, %s, %s, %s, %s)",
            (user_id, t_type, amount, category, desc)
        )
        
        icon = "📤 Pengeluaran" if t_type == "EXPENSE" else "📥 Pemasukan"
        saved_summary.append(f"{icon}: *Rp {amount:,.0f}*\n🏷️ Kategori: {category}\n📝 Ket: {desc}")

    conn.commit()
    cur.close()
    conn.close()

    return "✅ *Berhasil Dicatat via AI!*\n\n" + "\n\n".join(saved_summary)


def get_rekap(user_id: int) -> str:
    """Calculate totals and balance from database"""
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = %s AND type = 'INCOME'", (user_id,))
        total_income = cur.fetchone()[0]

        cur.execute("SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE user_id = %s AND type = 'EXPENSE'", (user_id,))
        total_expense = cur.fetchone()[0]

        balance = total_income - total_expense

        cur.close()
        conn.close()

        return (
            "📊 *Laporan Rekap Keuangan*\n\n"
            f"📥 Total Pemasukan: *Rp {total_income:,.0f}*\n"
            f"📤 Total Pengeluaran: *Rp {total_expense:,.0f}*\n"
            "-----------------------------------\n"
            f"💰 *Sisa Saldo: Rp {balance:,.0f}*"
        )
    except Exception as e:
        return f"❌ Gagal mengambil data rekap: {e}"


@app.post("/")
@app.post("/api/index")
async def telegram_webhook(request: Request):
    """Main Webhook Handler for Telegram"""
    try:
        data = await request.json()
        if "message" not in data:
            return {"status": "ok"}

        message = data["message"]
        chat_id = message["chat"]["id"]

        # 1. Handle Commands & Text Messages
        if "text" in message:
            text = message["text"].strip()

            if text == "/start":
                reply = (
                    "👋 *Selamat datang di Bot Catatan Keuangan AI!*\n\n"
                    "Kamu bisa mencatat keuangan secara alami tanpa command kaku:\n"
                    "• *Ketik Santai:* `Tadi makan siang nasi padang 25rb sama naik gojek 15k`\n"
                    "• *Kirim Foto Struk:* Cukup kirimkan foto struk belanjaanmu!\n"
                    "• *Cek Rekap:* Ketik `/rekap` untuk melihat sisa saldo."
                )
            elif text == "/rekap":
                reply = get_rekap(chat_id)
            else:
                # Process with Gemini NLP
                transactions = parse_with_gemini_text(text)
                reply = save_transactions(chat_id, transactions)

            await send_message(chat_id, reply)

        # 2. Handle Receipt Photo Uploads (OCR)
        elif "photo" in message:
            await send_message(chat_id, "🔍 *Menganalisis foto struk dengan AI...*")
            # Take the highest resolution photo (last element)
            photo_file_id = message["photo"][-1]["file_id"]
            img_bytes = await get_telegram_file_bytes(photo_file_id)
            
            transactions = parse_with_gemini_vision(img_bytes)
            reply = save_transactions(chat_id, transactions)
            await send_message(chat_id, reply)

    except Exception as e:
        print("Webhook Error:", e)

    return {"status": "ok"}