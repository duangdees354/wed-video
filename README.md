# MySeriesVideo v3.3

โปรเจกต์นี้เป็นเว็บดูวิดีโอแบบ Flask แยก `series` และ `episodes` พร้อมระบบสมาชิก, แอดมิน, อัปโหลดไฟล์, สำรองข้อมูล และ Cloudflare Turnstile สำหรับยืนยันว่าไม่ใช่บอท

## ฟีเจอร์หลัก

- จัดการเรื่องและตอนแยกกัน
- รองรับวิดีโอ 3 แบบ: ลิงก์ mp4, Google Drive, อัปโหลดไฟล์
- ระบบสมาชิกทั่วไปและประวัติการดู
- ระบบแอดมิน
- สำรอง/กู้คืนข้อมูลเป็น JSON
- รองรับ Cloudflare Turnstile ที่หน้า:
  - สมัครสมาชิก
  - เข้าสู่ระบบผู้ใช้
  - เข้าสู่ระบบแอดมิน
- รองรับ Railway Volume สำหรับเก็บข้อมูลถาวร:
  - `videos.db`
  - ไฟล์วิดีโอใน `video_files`
  - รูปปกที่อัปโหลด

## รันในเครื่อง

```bash
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

ถ้าต้องการทดสอบ Turnstile ในเครื่อง ให้สร้างไฟล์ `.env` จาก `.env.example` แล้วใส่ test keys หรือ production keys ของ Cloudflare
แอปจะอ่าน `.env` อัตโนมัติเมื่อรัน `python app.py`

## Environment Variables

ดูตัวอย่างได้ที่ `.env.example`

ตัวสำคัญ:

- `SECRET_KEY` คีย์ลับของ Flask session
- `TURNSTILE_SITE_KEY` site key จาก Cloudflare Turnstile
- `TURNSTILE_SECRET_KEY` secret key จาก Cloudflare Turnstile
- `TURNSTILE_REQUIRED=true` บังคับให้แอปไม่ยอมรันแบบไม่มี Turnstile ใน production
- `TURNSTILE_ALLOWED_HOSTNAMES` รายชื่อโดเมนที่ยอมรับได้ คั่นด้วย comma
- `DATA_DIR=/data` path สำหรับเก็บฐานข้อมูล/ไฟล์บน volume
- `WEB_CONCURRENCY`, `GUNICORN_THREADS`, `GUNICORN_TIMEOUT` ปรับ Gunicorn ได้ตามต้องการ

## Deploy บน Railway

ไฟล์ชุดนี้เตรียมให้ Railway ใช้งานได้แล้ว:

- มี `Procfile`
- มี `start.sh`
- รองรับ `PORT`
- รองรับ `X-Forwarded-*` ผ่าน `ProxyFix`
- รองรับ Railway Volume ผ่าน `DATA_DIR` หรือ `RAILWAY_VOLUME_MOUNT_PATH`

### ขั้นตอนแนะนำ

1. อัปโหลดโปรเจกต์นี้ขึ้น GitHub หรือ deploy ผ่าน Railway CLI
2. สร้าง Service ใหม่บน Railway จาก repo นี้
3. ผูก Volume ให้ Service และตั้ง mount path เป็น `/data`
4. ตั้งค่า Variables อย่างน้อยดังนี้

```env
SECRET_KEY=ใส่คีย์ลับแบบสุ่มยาว
TURNSTILE_SITE_KEY=site_key_จาก_cloudflare
TURNSTILE_SECRET_KEY=secret_key_จาก_cloudflare
TURNSTILE_REQUIRED=true
TURNSTILE_ALLOWED_HOSTNAMES=your-service.up.railway.app
DATA_DIR=/data
```

5. ในหน้า Networking ของ Railway กด Generate Domain หรือผูก Custom Domain
6. ใน Cloudflare Turnstile widget ให้เพิ่มโดเมนของ Railway หรือ custom domain ของคุณใน allowed hostnames
7. Deploy ได้เลย

## หมายเหตุสำคัญสำหรับ Railway

- ถ้าไม่ใช้ Volume, ฐานข้อมูล SQLite และไฟล์ที่อัปโหลดจะหายได้เมื่อมีการ redeploy/restart
- production ควรใช้ `SECRET_KEY` จริง ห้ามใช้ค่า default
- ถ้าตั้ง `TURNSTILE_REQUIRED=true` แต่ลืมใส่ keys แอปจะไม่ยอมรัน เพื่อกันเปิดเว็บแบบไม่มี bot protection โดยไม่รู้ตัว

## Start Command

Railway จะใช้คำสั่งนี้ผ่าน `Procfile`

```bash
sh start.sh
```

ซึ่งภายในจะรัน:

```bash
gunicorn app:app --bind 0.0.0.0:$PORT
```

## Cloudflare Turnstile

แอปนี้ตรวจ token ฝั่งเซิร์ฟเวอร์ผ่าน `Siteverify` ทุกครั้ง ไม่ได้เชื่อเฉพาะ widget ฝั่งหน้าเว็บ

แนะนำให้อ่านเอกสารทางการ:

- https://developers.cloudflare.com/turnstile/get-started/
- https://developers.cloudflare.com/turnstile/get-started/server-side-validation/
- https://developers.cloudflare.com/turnstile/troubleshooting/testing/

## Railway Docs

- https://docs.railway.com/guides/flask
- https://docs.railway.com/deploy/exposing-your-app
- https://docs.railway.com/guides/volumes
