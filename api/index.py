import os
import json
import io
import psycopg2
import httpx
from PIL import Image
from fastapi import FastAPI, Request
from google import genai

app = FastAPI()

# Configuration from Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

# Initialize Gemini Client
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


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


init_db()


# --- Telegram API Helpers ---

async def send_message(chat_id: int, text: str, reply_markup: dict = None):
    """Send text response to Telegram user with optional inline buttons"""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    async with httpx.AsyncClient() as http_client:
        await http_client.post(f"{TELEGRAM_API}/sendMessage", json=payload)


async def edit_message_text(chat_id: int, message_id: int, text: str):
    """Edit existing Telegram message text"""
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as http_client:
        await http_client.post(f"{TELEGRAM_API}/editMessageText", json=payload)


async def answer_callback_query(callback_query_id: str, text: str = None):
    """Acknowledge Telegram button click"""
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    async with httpx.AsyncClient() as http_client:
        await http_client.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload)


async def get_telegram_file_bytes(file_id: str) -> bytes:
    """Download image file from Telegram servers"""
    async with httpx.AsyncClient() as http_client:
        res = await http_client.get(f"{TELEGRAM_API}/getFile", params={"file_id": file_id})
        file_path = res.json()["result"]["file_path"]
        download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        file_res = await http_client.get(download_url)
        return file_res.content


# --- Gemini AI Integration ---

def parse_with_gemini_text(user_text: str) -> list:
    """Parse text using Gemini AI into structured transaction JSON"""
    if not GEMINI_API_KEY or not client:
        print("Error: GEMINI_API_KEY belum terpasang!")
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
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=prompt,
        )
        text_resp = response.text.strip()
        if "```" in text_resp:
            text_resp = text_resp.split("```")[1]
            if text_resp.startswith("json"):
                text_resp = text_resp[4:]
        return json.loads(text_resp.strip())
    except Exception as e:
        print("Gemini Text Exception Detail:", e)
        return []


def parse_with_gemini_vision(image_bytes: bytes) -> list:
    """Parse photo receipt using Gemini Vision OCR"""
    if not GEMINI_API_KEY or not client:
        print("Error: GEMINI_API_KEY belum terpasang!")
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
        response = client.models.generate_content(
            model="gemini-3.6-flash",
            contents=[image, prompt],
        )
        text_resp = response.text.strip()
        if "```" in text_resp:
            text_resp = text_resp.split("```")[1]
            if text_resp.startswith("json"):
                text_resp = text_resp[4:]
        return json.loads(text_resp.strip())
    except Exception as e:
        print("Gemini Vision Exception Detail:", e)
        return []


# --- Database CRUD Operations ---

def save_transactions(user_id: int, transactions: list):
    """Save extracted transactions to Neon Postgres DB and build Inline Keyboard"""
    if not transactions:
        return "❌ Gagal memproses transaksi. Pastikan format teks atau foto jelas.", None

    conn = get_db()
    cur = conn.cursor()
    saved_summary = []
    keyboard_buttons = []

    for item in transactions:
        t_type = item.get("type", "EXPENSE")
        amount = item.get("amount", 0)
        category = item.get("category", "Umum")
        desc = item.get("description", "-")

        cur.execute(
            "INSERT INTO transactions (user_id, type, amount, category, description) VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (user_id, t_type, amount, category, desc)
        )
        inserted_id = cur.fetchone()[0]

        icon = "📤 Pengeluaran" if t_type == "EXPENSE" else "📥 Pemasukan"
        saved_summary.append(f"{icon}: *Rp {amount:,.0f}*\n🏷️ Kategori: {category}\n📝 Ket: {desc}")

        short_desc = desc[:12] if len(desc) > 12 else desc
        keyboard_buttons.append([
            {"text": f"❌ Hapus: {short_desc} (Rp {amount:,.0f})", "callback_data": f"delete_{inserted_id}"}
        ])

    conn.commit()
    cur.close()
    conn.close()

    reply_markup = {"inline_keyboard": keyboard_buttons} if keyboard_buttons else None
    return "✅ *Berhasil Dicatat via AI!*\n\n" + "\n\n".join(saved_summary), reply_markup


def delete_transaction_by_id(user_id: int, tx_id: int) -> bool:
    """Delete a specific transaction by ID for a user"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id))
        deleted_count = cur.rowcount
        conn.commit()
        cur.close()
        conn.close()
        return deleted_count > 0
    except Exception as e:
        print("Delete Tx Error:", e)
        return False


def delete_last_transaction(user_id: int) -> str:
    """Delete the single most recent transaction of the user"""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT id, amount, category, description FROM transactions WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT 1",
            (user_id,)
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            conn.close()
            return "ℹ️ Tidak ada transaksi terakhir yang ditemukan untuk dibatalkan."

        tx_id, amount, category, desc = row
        cur.execute("DELETE FROM transactions WHERE id = %s AND user_id = %s", (tx_id, user_id))
        conn.commit()
        cur.close()
        conn.close()

        return f"🗑️ *Transaksi Terakhir Berhasil Dibatalkan!*\n💰 *Rp {amount:,.0f}* ({category} - {desc})"
    except Exception as e:
        return f"❌ Gagal membatalkan transaksi: {e}"


def update_last_transaction_amount(user_id: int, new_amount_text: str) -> str:
    """Update amount for the most recent transaction of the user"""
    parsed = parse_with_gemini_text(f"transaksi {new_amount_text}")
    if not parsed or "amount" not in parsed[0]:
        return "❌ Nominal baru tidak valid. Contoh penggunaan: `/revisi 25rb` atau `/revisi 25000`"

    new_amount = parsed[0]["amount"]

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE transactions 
            SET amount = %s 
            WHERE id = (
                SELECT id FROM transactions 
                WHERE user_id = %s 
                ORDER BY created_at DESC, id DESC LIMIT 1
            )
            RETURNING category, description;
            """,
            (new_amount, user_id)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()

        if not row:
            return "ℹ️ Tidak ada transaksi terakhir yang ditemukan untuk diperbarui."

        category, desc = row
        return (
            "✏️ *Nominal Berhasil Diperbarui!*\n"
            f"📝 *{desc}* ({category})\n"
            f"💰 Nominal Baru: *Rp {new_amount:,.0f}*"
        )
    except Exception as e:
        return f"❌ Gagal memperbarui nominal: {e}"


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


def get_riwayat(user_id: int) -> str:
    """Fetch 5 most recent transactions from database"""
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT type, amount, category, description, created_at 
            FROM transactions 
            WHERE user_id = %s 
            ORDER BY created_at DESC 
            LIMIT 5
            """, 
            (user_id,)
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        if not rows:
            return "ℹ️ Belum ada transaksi yang dicatat."

        pesan = "📜 *5 Transaksi Terakhir Kamu:*\n\n"
        
        for row in rows:
            t_type, amount, category, desc, created_at = row
            icon = "📤" if t_type == "EXPENSE" else "📥"
            tanggal = created_at.strftime("%d-%m-%Y %H:%M")
            
            pesan += f"{icon} *Rp {amount:,.0f}* ({category})\n"
            pesan += f"📝 {desc}\n"
            pesan += f"📅 {tanggal}\n"
            pesan += "-----------------------------------\n"
            
        return pesan
    except Exception as e:
        return f"❌ Gagal mengambil riwayat transaksi: {e}"


# --- Main Webhook Handler ---

@app.post("/")
@app.post("/api/index")
async def telegram_webhook(request: Request):
    """Main Webhook Handler for Telegram"""
    try:
        data = await request.json()

        # 1. Handle Inline Button Clicks (Callback Queries)
        if "callback_query" in data:
            cb = data["callback_query"]
            cb_id = cb["id"]
            user_id = cb["from"]["id"]
            chat_id = cb["message"]["chat"]["id"]
            message_id = cb["message"]["message_id"]
            cb_data = cb.get("data", "")

            if cb_data.startswith("delete_"):
                tx_id = int(cb_data.split("_")[1])
                success = delete_transaction_by_id(user_id, tx_id)

                if success:
                    await answer_callback_query(cb_id, "✅ Transaksi berhasil dihapus!")
                    await edit_message_text(chat_id, message_id, "❌ *[TRANSAKSI DIHAPUS]* Transaksi ini telah dibatalkan.")
                else:
                    await answer_callback_query(cb_id, "⚠️ Transaksi gagal dihapus atau sudah tidak ada.")

            return {"status": "ok"}

        # 2. Handle Text Messages & Photo Uploads
        if "message" in data:
            message = data["message"]
            chat_id = message["chat"]["id"]

            if "text" in message:
                text = message["text"].strip()

                if text == "/start":
                    reply = (
                        "👋 *Selamat datang di Bot Catatan Keuangan AI!*\n\n"
                        "Kamu bisa mencatat keuangan secara alami tanpa command kaku:\n"
                        "• *Ketik Santai:* `Tadi makan siang nasi padang 25rb`\n"
                        "• *Kirim Foto Struk:* Kirimkan foto struk belanjaanmu!\n"
                        "• *Revisi Nominal:* `/revisi 25rb` (mengubah nominal terakhir)\n"
                        "• *Batal Transaksi:* `/batal` atau tekan tombol hapus.\n"
                        "• *Lihat Riwayat:* `/riwayat` (5 transaksi terakhir)\n"
                        "• *Cek Rekap:* `/rekap` (sisa saldo & total)"
                    )
                    await send_message(chat_id, reply)

                elif text.startswith("/revisi"):
                    parts = text.split(maxsplit=1)
                    if len(parts) > 1:
                        reply = update_last_transaction_amount(chat_id, parts[1])
                    else:
                        reply = "⚠️ Masukkan nominal baru. Contoh: `/revisi 25rb` atau `/revisi 25000`"
                    await send_message(chat_id, reply)

                elif text in ["/batal", "/undo"]:
                    reply = delete_last_transaction(chat_id)
                    await send_message(chat_id, reply)

                elif text == "/rekap":
                    reply = get_rekap(chat_id)
                    await send_message(chat_id, reply)

                elif text == "/riwayat":
                    reply = get_riwayat(chat_id)
                    await send_message(chat_id, reply)

                else:
                    transactions = parse_with_gemini_text(text)
                    reply, markup = save_transactions(chat_id, transactions)
                    await send_message(chat_id, reply, reply_markup=markup)

            elif "photo" in message:
                await send_message(chat_id, "🔍 *Menganalisis foto struk dengan AI...*")
                photo_file_id = message["photo"][-1]["file_id"]
                img_bytes = await get_telegram_file_bytes(photo_file_id)

                transactions = parse_with_gemini_vision(img_bytes)
                reply, markup = save_transactions(chat_id, transactions)
                await send_message(chat_id, reply, reply_markup=markup)

    except Exception as e:
        print("Webhook Error:", e)

    return {"status": "ok"}