# MySeriesVideo v3.4

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

เนื่องจาก `TURNSTILE_REQUIRED` มีค่าเริ่มต้นเป็น `true` หากรันโดยไม่ได้ตั้งค่า Turnstile
แอปจะหยุดทำงานทันที ให้สร้างไฟล์ `.env` จาก `.env.example` และทำอย่างใดอย่างหนึ่ง:

- **ปิดการบังคับ** (สำหรับ local dev): ตั้ง `TURNSTILE_REQUIRED=false`
- **เปิดใช้งานเต็มรูปแบบ**: ใส่ `TURNSTILE_SITE_KEY` และ `TURNSTILE_SECRET_KEY` จาก Cloudflare

## Environment Variables

ดูตัวอย่างได้ที่ `.env.example`

| ตัวแปร | คำอธิบาย | ค่าเริ่มต้น |
|---|---|---|
| `SECRET_KEY` | คีย์ลับของ Flask session | `dev-secret-key` (ห้ามใช้ใน production) |
| `TURNSTILE_SITE_KEY` | Site key จาก Cloudflare Turnstile | — |
| `TURNSTILE_SECRET_KEY` | Secret key จาก Cloudflare Turnstile | — |
| `TURNSTILE_REQUIRED` | `true` = บังคับใช้ Turnstile, `false` = ปิดการบังคับ | **`true`** |
| `TURNSTILE_ALLOWED_HOSTNAMES` | รายชื่อโดเมนที่ยอมรับได้ คั่นด้วย comma | — (ยอมรับทุกโดเมน) |
| `DATA_DIR` | Path สำหรับเก็บฐานข้อมูล/ไฟล์บน volume | ไดเรกทอรีของแอป |
| `WEB_CONCURRENCY` | จำนวน Gunicorn worker processes | `2` |
| `GUNICORN_THREADS` | จำนวน threads ต่อ worker | `2` |
| `GUNICORN_TIMEOUT` | Request timeout (วินาที) | `120` |

### การควบคุม Turnstile ผ่าน `TURNSTILE_REQUIRED`

ค่าเริ่มต้นของ `TURNSTILE_REQUIRED` คือ **`true`** ทุกสภาพแวดล้อม (ไม่ขึ้นกับว่ารันใน production หรือ local)

```
# ปิดการบังคับ — รันได้โดยไม่ต้องมี Turnstile keys (เหมาะสำหรับ development)
TURNSTILE_REQUIRED=false

# บังคับใช้ (ค่าเริ่มต้น) — แอปจะไม่รันหากไม่มี keys ทั้งสองตัว
TURNSTILE_REQUIRED=true
```

> **หมายเหตุ:** แม้ตั้ง `TURNSTILE_REQUIRED=true` แล้ว widget จะแสดงก็ต่อเมื่อ `TURNSTILE_SITE_KEY` ถูกกำหนดเท่านั้น และการตรวจสอบฝั่งเซิร์ฟเวอร์จะทำงานก็ต่อเมื่อ `TURNSTILE_SECRET_KEY` ถูกกำหนดด้วยเช่นกัน

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
4. ตั้งค่า Variables อย่างน้อยดังนี้:

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
- ตั้งแต่ v3.4 เป็นต้นมา `TURNSTILE_REQUIRED` มีค่าเริ่มต้นเป็น `true` เสมอ ไม่ว่าจะเป็น local หรือ production — หากต้องการรันโดยไม่มี Turnstile ต้องตั้ง `TURNSTILE_REQUIRED=false` อย่างชัดเจน

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

## Changelog

### v3.4
- `TURNSTILE_REQUIRED` มีค่าเริ่มต้นเป็น `true` เสมอ (เปลี่ยนจากเดิมที่ขึ้นกับ `IS_PRODUCTION`)
  - ก่อนหน้านี้: บังคับเฉพาะเมื่อตรวจพบว่ารันบน Railway หรือตั้ง `IS_PRODUCTION=true`
  - ตอนนี้: บังคับทุกสภาพแวดล้อมโดย default — หากไม่ต้องการบังคับต้องตั้ง `TURNSTILE_REQUIRED=false` ชัดเจน
- อัปเดต `.env.example` เพิ่มคำอธิบาย `TURNSTILE_REQUIRED` ให้ครบถ้วน
