# 💧 Water Sentinel

ระบบตรวจจับความผิดปกติของน้ำแบบเรียลไทม์ ด้วย ESP32 + เซนเซอร์อุณหภูมิ, TDS, ความขุ่น และ pH (เลือกได้)
เรียนรู้จากน้ำปกติอย่างเดียว (unsupervised, Normal Operating Condition) แล้วเตือนทันทีเมื่อน้ำเปลี่ยน
พร้อมหน้าเว็บที่ดูจากมือถือได้ และแจ้งเตือนเข้ามือถือ

Course project: 2306565 Digital Lab, Chemical Engineering, Chulalongkorn University

## เริ่มใช้งาน (Windows)

1. ติดตั้ง Python 3.11+ (ติ๊ก *Add to PATH*)
2. อัปโหลด `firmware/water_sentinel/water_sentinel.ino` ลง ESP32 (Arduino IDE, ไลบรารี OneWire + DallasTemperature)
3. ดับเบิลคลิก **`start.bat`** ครั้งแรกจะติดตั้งไลบรารีให้เอง แล้วเปิดเบราว์เซอร์ที่ http://127.0.0.1:8000
4. ไม่มีบอร์ด? ดับเบิลคลิก **`scripts/start_sim.bat`** เพื่อลองทุกอย่างด้วยข้อมูลจำลอง

> ปิด Arduino Serial Monitor ก่อนเสมอ เพราะพอร์ต USB ใช้ได้ทีละโปรแกรม

## ขั้นตอนการทำงาน

| ขั้น | แท็บ | ทำอะไร |
|---|---|---|
| 1 | เก็บข้อมูล | เก็บน้ำปกติ ≥ 5 รอบ (แก้วละรอบ รอบละ 5 นาที) และ anomaly รอบละ 3 นาที (เกลือ, แป้ง, น้ำร้อน, ...) |
| 2 | เทรน & เปรียบเทียบ | กดปุ่มเดียว เทรนและประเมิน 5 โมเดล ระบบเลือกตัวที่ดีที่สุดให้ · หรือเลือกเองว่ารอบไหนใช้เทรน/กันไว้ทดสอบ เซนเซอร์ไหน โมเดลไหน แล้วบันทึกเป็น "ชุดข้อมูล" |
| 3 | ภาพรวม | เปิด **โหมดเฝ้าระวัง** ทิ้งไว้ จุ่มหัววัด ถ้าผิดปกติจะเตือนทันที (เสียง + เบราว์เซอร์ + มือถือ) |
| 3b | ภาพรวม → ข้อมูลที่บันทึกไว้ | เลือกหลายรอบมาซ้อนดู เทียบน้ำปกติกับน้ำเติมสาร ดูค่าเฉลี่ย/noise/แนวโน้ม ส่งออก PNG และ CSV |
| 4 | Visualization | คะแนนตามเวลา, ROC, scatter + ขอบเขตน้ำปกติ, PCA / t-SNE / UMAP |

**session = น้ำหนึ่งชุด**: รอบที่เก็บต่อกันโดยไม่เปลี่ยนน้ำนับเป็น session เดียว (ติ๊ก "เปลี่ยนน้ำใหม่แล้ว" ทุกครั้งที่เปลี่ยนน้ำ) ระบบประเมินโดยกันทั้ง session ออก จึงต้องมีน้ำปกติอย่างน้อย 3 session

ข้อมูลสดทุกค่าถูกเก็บใน `runtime/logs/stream-*.csv` และทุกครั้งที่เตือน ระบบบันทึกช่วงนั้นเป็นรอบ `unlabeled` ให้เอง ไปตั้งชื่อที่แท็บเก็บข้อมูลแล้วใช้เทรนได้

เทรนครั้งเดียวแล้วเปิดเฝ้าระวังทิ้งไว้ได้ โปรแกรมจำสถานะไว้ เปิดใหม่ก็กลับมาเฝ้าระวังต่อเอง
`scripts/autostart.bat` ตั้งให้เปิดเองตอนเข้า Windows

## ความขุ่นเป็น NTU

เซนเซอร์ให้แรงดัน ไม่ใช่ NTU จริง: ตั้งค่า → ความขุ่น → จุ่มในน้ำใสแล้วกด "ตั้งเป็นน้ำใส" ระบบจะแสดงเป็น NTU (ขึ้นต้นว่า "ประมาณ" จนกว่าจะเพิ่มจุดมาตรฐานของคุณเอง) ค่าต่ำกว่าที่เซนเซอร์แยกได้จะแสดงเป็น `< N NTU` โมเดลตรวจจับยังใช้แรงดันดิบเหมือนเดิม

## สาเหตุที่เป็นไปได้ (ทดลอง)

เมื่อเตือน ระบบดู "ทิศทางที่เซนเซอร์เปลี่ยน" แล้วบอกว่าสอดคล้องกับสารกลุ่มไหน (เกลือ, ด่าง, กรด, ของแข็งแขวนลอย, น้ำอุ่น/เย็น, หัววัดถูกรบกวน) โดยเริ่มจากกฎทั่วไป และเรียนรู้จากรอบ anomaly ที่คุณตั้งชื่อไว้ ถ้าไม่ตรงกับอะไรเลยจะบอกว่า "ไม่ทราบสาเหตุ" ไม่ใช่การยืนยันว่าเป็นสารอะไร และไม่ได้บอกว่าน้ำปลอดภัยหรือไม่ เซนเซอร์ 3 ตัวแยกเกลือธรรมดากับเบกกิ้งโซดาไม่ได้ (ต้องมี pH)

## ใช้ pH ด้วย (4 เซนเซอร์)

- ต่อ Po ของบอร์ด pH ผ่านตัวแบ่งแรงดัน 10k/10k เข้า D35 (ห้ามต่อตรงเมื่อบอร์ด pH ใช้ไฟ 5 V)
- ตั้งค่า → เปิด "ใช้เซนเซอร์ pH" → คาลิเบรต pH สองจุด (เช่น 6.86 และ 4.01)
- เก็บข้อมูลใหม่ (ไฟล์เก่าที่ไม่มี pH จะถูกข้ามอัตโนมัติ) แล้วเทรนแบบ 4 เซนเซอร์
- หัววัด pH กับ TDS ในน้ำเดียวกันรบกวนกันได้ ดูตัวเลือก `TDS_POWER_PIN` ใน firmware

## อัลกอริทึม

ทุกโมเดลใช้ขั้นตอนเดียวกัน จึงเทียบกันได้ยุติธรรม:

1. **แยกเซนเซอร์เสียออกจากน้ำเสีย**: หัววัดพ้นน้ำ, ADC อิ่มตัว, เซนเซอร์หลุด → แจ้งเป็น "เซนเซอร์มีปัญหา" ไม่นับเป็นน้ำผิดปกติ
2. **Rolling median 5 ค่า** ตัดสัญญาณกระชากจากฟองอากาศ
3. **โมเดล** ให้คะแนนความผิดปกติ
4. **เส้นเตือนจาก cross-validation ระหว่างรอบ** (รวมความต่างระหว่างแก้ว/วันไว้แล้ว) และเตือนเมื่อเกินติดกัน 3 ค่า มี hysteresis ตอนเลิกเตือน

| โมเดล | แนวคิด |
|---|---|
| **MSPC** (ค่าเริ่มต้น) | Robust Mahalanobis distance + EWMA: มาตรฐานงาน process monitoring สำหรับเซนเซอร์ไม่กี่ตัวที่สัมพันธ์กัน |
| PCA (T² + Q) | ลดมิติแล้วดูระยะในและนอกระนาบหลัก |
| Isolation Forest | ต้นไม้สุ่มแยกจุดโดดเดี่ยว |
| One-Class SVM | ขอบเขตน้ำปกติแบบ kernel |
| Local Outlier Factor | เทียบความหนาแน่นกับเพื่อนบ้าน |

การประเมิน: **leave-one-run-out** แต่ละรอบ normal ถูกกันไว้ทดสอบทีละรอบ ตัวชี้วัด = false alarm, detection, balanced accuracy, ROC-AUC, เวลาจนเตือน, ความเร็ว

## แจ้งเตือนเข้ามือถือ

- **ntfy** (แนะนำ ไม่ต้องสมัคร): ติดตั้งแอป ntfy → subscribe ชื่อ topic เดียวกับในหน้าตั้งค่า
- **Telegram**: bot token + chat id
- **LINE**: LINE Messaging API (LINE Notify ปิดบริการไปแล้ว)

## ดูจากมือถือที่ไหนก็ได้

ติดตั้ง [Tailscale](https://tailscale.com) ในคอมที่ต่อ ESP32 และในมือถือ (บัญชีเดียวกัน) → ตั้งค่า → เปิด "อนุญาตเข้าจากเครื่องอื่น" + ตั้ง PIN → รีสตาร์ทโปรแกรม → เปิด `http://<IP Tailscale ของคอม>:8000` จากมือถือ

## ให้คนอื่นดู (พรีเซนต์)

ตั้งค่า → การ์ด **"ลิงก์ให้คนดู"** → กด "เริ่มลิงก์สาธารณะ" ได้ลิงก์ `https://….trycloudflare.com` และปุ่ม **QR เต็มจอ** ให้คนในห้องสแกน (ปุ่ม "📱 QR ให้คนดู" อยู่ที่หน้าภาพรวมด้วย)

- ครั้งแรกต้องติดตั้ง cloudflared: `winget install --id Cloudflare.cloudflared` แล้วเปิดโปรแกรมใหม่
- คนดูเห็นค่าสด กราฟ ผลโมเดล ข้อมูลที่บันทึก แต่ **แก้ไขอะไรไม่ได้** (เซิร์ฟเวอร์ปฏิเสธเอง ไม่ใช่แค่ซ่อนปุ่ม) และไม่เห็นค่าลับ (PIN, token, ntfy topic)
- ผู้ดูแลเข้าสู่ระบบผ่านลิงก์เดียวกันด้วย PIN (อย่างน้อย 6 ตัว ใส่ผิด 8 ครั้งล็อก 15 นาที)
- ลิงก์เปลี่ยนทุกครั้งที่เริ่มใหม่ อยากได้ลิงก์คงที่ใช้ `tailscale funnel --bg 8000` แล้วใส่ที่อยู่ในการ์ดเดียวกัน
- คอมต้องเปิดโปรแกรมค้างไว้ระหว่างพรีเซนต์ ปิดโปรแกรม = ลิงก์ปิดด้วย

## Board-only mode (ESP32 detects by itself) — simulate it first

The ESP32 will run MSPC itself, alert via ntfy, and push a summary to ThingSpeak every 15 s. Until the
firmware is ready, `py -m sentinel edge-sim` pretends to be the board on this laptop (same maths, tested
against the app).

1. ThingSpeak: create a channel with 8 fields (temp, TDS, turbidity V, pH, score, status, driver, driver z).
2. Copy `runtime/edge_secrets.example.json` to `runtime/edge_secrets.json` and paste the Write API key and an ntfy topic.
3. `py -m sentinel edge-sim` (type `salt`, `starch`, `normal` + Enter to change the water), or
   `py -m sentinel edge-sim --script normal:120,salt:90,normal:120`. `--dry-run` sends nothing.

`py -m sentinel export-edge` writes `runtime/model/edge_model.json` and `runtime/model/edge_model.h` for the firmware.

## Command line

```
py run.py --sim                 เปิดแบบจำลอง
py -m sentinel train            เทรน + เปรียบเทียบโมเดลจาก runtime/data/
py -m sentinel train --ph       เทรนแบบ 4 เซนเซอร์
py -m sentinel train --dataset NAME   เทรนจากชุดข้อมูลที่บันทึกไว้ (runtime/data/datasets.json)
py -m sentinel edge-sim         ESP32 simulator → ThingSpeak + ntfy
py -m sentinel test runtime\data\run006_salt_lvl1_....csv
py -m pytest -q                 ทดสอบทั้งระบบด้วยตัวจำลอง
```
API docs อัตโนมัติ: http://127.0.0.1:8000/docs

## โครงสร้าง

```
02_software/
├── start.bat              ดับเบิลคลิกเพื่อเปิดโปรแกรม (จุดเริ่มเดียวที่ต้องจำ)
├── run.py                 จุดเริ่มโปรแกรม (start.bat เรียกตัวนี้)
├── requirements.txt       ไลบรารี Python
├── README.md  CHANGELOG.md
│
├── sentinel/              Backend (Python package) แยกตามหน้าที่
│   ├── config.py          ค่าตั้งต้นทั้งหมด + ที่อยู่โฟลเดอร์ (ค่าจริงอยู่ใน runtime/config.json)
│   ├── __main__.py        คำสั่ง `py -m sentinel ...` (train, test, edge-sim, export-edge)
│   ├── acquisition/       อ่านเซนเซอร์
│   │   ├── sources.py       USB serial, ตัวจำลอง
│   │   ├── features.py      แรงดัน → ค่าที่ใช้ (TDS ppm, pH), ตรวจเซนเซอร์เสีย
│   │   └── turbidity.py     แรงดันความขุ่น → NTU, ลิมิตการตรวจวัด
│   ├── storage/           ข้อมูลที่บันทึกไว้
│   │   ├── runs.py          ไฟล์รอบ + metadata, session
│   │   ├── datasets.py      role ของแต่ละรอบ, ชุดข้อมูลที่ตั้งชื่อ
│   │   └── explorer.py      รายละเอียดรอบ, ค่าลักษณะ, ย้ายเก็บ/นำกลับ
│   ├── detection/         ตรวจจับความผิดปกติ
│   │   ├── models.py        5 โมเดล + ให้คะแนน/เส้นเตือน
│   │   ├── training.py      เทรน, ประเมิน, embedding (PCA/t-SNE/UMAP)
│   │   ├── tuning.py        ปรับ hyperparameter, what-if
│   │   ├── drift.py         CUSUM, Theil-Sen, change point
│   │   ├── causes.py        สาเหตุที่เป็นไปได้ (ทดลอง)
│   │   └── live.py          ให้คะแนนแบบเรียลไทม์
│   ├── edge/              MSPC บน ESP32
│   │   ├── export.py        ส่งออกโมเดลเป็น JSON + C header
│   │   └── simulator.py     จำลองบอร์ด → ThingSpeak + ntfy
│   └── app/               โปรแกรมที่รันอยู่
│       ├── hub.py           สถานะกลาง, บันทึกข้อมูล, เหตุการณ์
│       ├── server.py        FastAPI + ความปลอดภัย
│       ├── alerts.py        ntfy / Telegram / LINE
│       └── share.py         ลิงก์สาธารณะ (cloudflared)
│
├── frontend/
│   ├── app/               หน้าเว็บบนคอม (HTML/CSS/JS + Chart.js)
│   └── dashboard/         หน้าเว็บสาธารณะบน Netlify (อ่าน ThingSpeak)
├── firmware/              โค้ด ESP32 (Arduino sketch)
├── tests/                 ทดสอบ end-to-end (`py -m pytest -q`)
├── docs/                  ARCHITECTURE.md, แผนงาน
├── scripts/               start_sim.bat, autostart.bat
└── runtime/               ข้อมูลที่โปรแกรมเขียน (ไม่ขึ้น git): config.json, edge_secrets.json, data/, model/, logs/
```
