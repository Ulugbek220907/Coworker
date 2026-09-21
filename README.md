# Coworker

Uyda qolgan noutbukdagi hujjatni Telegram orqali topib beradigan yordamchi.

Dadam noutbukni ish joyiga olib borishni unutsa, menga qo'ng'iroq qilib
"falon faylni topib yubor" deb aytardi. Coworker shu ishni o'zi qiladi:
dadam botga oddiy tilda yozadi yoki ovozli xabar yuboradi, uydagi noutbuk
faylni topib, to'g'ridan-to'g'ri telefoniga yuboradi.

```
  Telefon                    Render (bepul)              Uydagi noutbuk
 ┌─────────┐   webhook     ┌────────────────┐   WSS    ┌──────────────────┐
 │ Telegram│ ────────────► │  relay server  │ ◄──────► │  Coworker agent  │
 │   bot   │ ◄──────────── │  (faqat pochta)│          │  · fayl qidiruv  │
 └─────────┘   xabar+fayl  └────────────────┘          │  · AI miyasi     │
                                                       │  · ovoz → matn   │
                                                       └──────────────────┘
```

Server **faqat pochtachi**: u hech qanday faylni saqlamaydi, diskni ko'rmaydi
va AI kalitini bilmaydi. Butun aql noutbukda ishlaydi. Shuning uchun Render'ning
bepul 512 MB'lik tarifi yetarli, va hujjatlaringiz uchinchi serverga tushmaydi.

---

## Nega bunday qurilgan

| Qaror | Sabab |
|---|---|
| Noutbuk **o'zi** serverga ulanadi (WebSocket) | Router sozlash, port ochish, statik IP kerak emas — har qanday uy internetida ishlaydi |
| Ma'lumotlar bazasi yo'q | Agent har ulanganda ishonchli chatlar ro'yxatini o'zi aytadi. Render qayta ishga tushsa — hech narsa yo'qolmaydi |
| AI noutbukda | Fayl mazmuni serverga chiqmaydi; Render bepul tarifi yengil qoladi |
| Indeks oldindan qurilmaydi | AI diskni odam kabi bosqichma-bosqich o'rganadi va savol beradi |
| Ovoz noutbukda taniladi | Bepul, internetsiz, API to'lovsiz |

---

## 1-qadam — Serverni Render'ga qo'yish

1. Render'da **New → Blueprint** → shu repozitoriyni tanlang.
   `render.yaml` hamma narsani o'zi sozlaydi.
2. Faqat bitta o'zgaruvchini qo'lda kiriting:

   | Nom | Qiymat |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | @BotFather bergan token |

   `WEBHOOK_SECRET` va `RELAY_TOKEN` avtomatik yaratiladi.
3. Deploy tugagach manzilni nusxalang, masalan
   `https://coworker-relay.onrender.com`.
   Webhook avtomatik ro'yxatdan o'tadi — qo'shimcha ish yo'q.

> **Bepul tarif haqida:** Render bepul servisni 15 daqiqa jimlikdan keyin
> uxlatadi. Agent har 4 daqiqada `/healthz` ga ping yuboradi, shuning uchun
> noutbuk yoqiq ekan server uxlamaydi. Agar baribir uxlab qolsa, birinchi
> xabar ~50 soniya kechikadi, keyin normal ishlaydi.

## 2-qadam — Noutbukka ilovani o'rnatish

```bash
git clone https://github.com/Ulugbek220907/Coworker.git
cd Coworker/agent
pip install -r requirements.txt
python run.py
```

Ochilgan oynada **Sozlamalar** bo'limini to'ldiring:

- **Server manzili** — Render bergan URL
- **AI modeli** — ro'yxatdan tanlang, API kalitni qo'ying
- **Server maxfiy kaliti** — Render'dagi `RELAY_TOKEN` qiymati
- **Qidiruv doirasi** — bo'sh qoldirsangiz barcha disklar qidiriladi

**Saqlash** → ilovani qayta oching.

## 3-qadam — Telefonni ulash

Ilova oynasida 6 xonali kod chiqadi. Telegramda botga yuboring:

```
/connect 967241
```

Tamom. Endi shunchaki yozish mumkin.

---

## Qanday ishlatiladi

```
  Dada: tekstil zavodi bilan shartnoma kerak edi
    AI: ✅ Yuborildi: Шартнома_Текстил_завод_2026.txt   [📎 fayl]

  Dada: hisobot kerak edi
    AI: Qaysi hisobot kerak?
        [ Йиллик_хисобот_2026.txt ]
        [ Налоговый_отчет_Q1.txt  ]

  Dada: (ovozli xabar) "o'tgan oygi buxgalteriya hisoboti"
    AI: 🎤 «o'tgan oygi buxgalteriya hisoboti»
        ✅ Yuborildi: Ҳисобот_август.xlsx   [📎 fayl]
```

Buyruqlar: `/status` · `/forget` (suhbatni tozalash) · `/disconnect`

### Fonda ishlash

Oynaning **X** tugmasi ilovani yopmaydi — tray'ga yashiradi va agent ishlashda
davom etadi. Butunlay chiqish uchun tray belgisiga o'ng tugma → **Chiqish**.

Sozlamalarda **«Kompyuter yonganda o'zi ishga tushsin»** ni yoqsangiz, noutbuk
har yonganda Coworker o'zi ishga tushadi — ya'ni dadangiz ilovani ochishni
eslab qolishi shart emas. Bu Windows'ning `HKCU\...\Run` kalitiga yoziladi,
admin huquqi talab qilmaydi.

> Windows xizmati (service) sifatida emas, oddiy foydalanuvchi ilovasi sifatida
> ishlaydi — bu ataylab: xizmat session 0 da, ekransiz ishlaydi.

---

## Uch til, bitta qidiruv

Papkalar kirillcha, so'rov lotincha bo'lishi mumkin — muhim emas.
Hammasi bitta shaklga keltiriladi, so'ng tarjima lug'ati qo'shiladi:

| So'rov | Topadi |
|---|---|
| `tekstil shartnoma` | `Договор_текстиль_2024.docx` |
| `договор текстиль` | `Shartnoma_tekstil_zavod.docx` |
| `zavod hisoboti` | `Отчет_фабрика_2024.xlsx` |
| `shartnomani` | `Шартнома...` (qo'shimchalar kesiladi) |

`shartnoma = договор = kontrakt`, `hisobot = отчет`, `zavod = фабрика` —
25 ga yaqin guruh [`textutil.py`](agent/coworker/textutil.py) ichida.

## Xotira — faqat oxirgi xabarga qaramaydi

Har so'rovda AI to'rt qatlamni ko'radi:

1. **Eslab qolingan ma'lumotlar** — "zavod deganda Tekstil zavodini nazarda
   tutaman" degan gap abadiy saqlanadi
2. **Suhbat xulosasi** — eski xabarlar o'chirilmaydi, qisqartiriladi
3. **Yaqinda yuborilgan fayllar** — "o'shani yana yubor" shuning uchun ishlaydi
4. **Oxirgi 14 ta xabar** — to'liq holicha

Bundan tashqari muvaffaqiyatli topilgan papka eslab qolinadi va keyingi safar
birinchi bo'lib qaraladi — ya'ni ilova ishlatilgani sayin tezlashadi.

## Ovozli xabar

Ikkalasi ham bepul, ochiq kodli va **internetsiz, noutbukning o'zida** ishlaydi:

| Engine | Hajmi | Kuchli tomoni |
|---|---|---|
| **faster-whisper** (standart) | ~145 MB (`base`) | Tilni o'zi aniqlaydi — o'zbekcha va ruschani aralash gapirsa ham tushunadi |
| **vosk** | ~50 MB | Juda yengil, eski noutbukda ham tez. Alohida o'zbekcha modeli bor |

Model birinchi ovozli xabarda avtomatik yuklanadi. Kerak bo'lmasa
Sozlamalarda `off` qilib qo'ying.

```bash
pip install faster-whisper av    # standart
pip install vosk                 # yengilroq muqobil
```

## Hujjat bilan ishlash (sichqonchasiz)

Agent nafaqat topadi, balki hujjat ustida ish ham qila oladi — **sichqonchaga
tegmasdan, skrinshotsiz, ekran qulflangan bo'lsa ham**. Bu Microsoft'ning UFO
tadqiqotidagi qoida: *avval API, GUI — oxirgi chora*.

| Asbob | Nima qiladi | Kerak bo'ladigan narsa |
|---|---|---|
| `sheet_read` | Excel jadvalini raqamlari bilan o'qiydi | hech narsa (openpyxl) |
| `sheet_list` | Varaqlar ro'yxati | hech narsa |
| `pdf_pages` | PDF'dan kerakli betlarni ajratadi | PyMuPDF |
| `to_pdf` | Word/Excel → PDF | MS Office **yoki** LibreOffice |
| `sheet_write` | Kataklarni o'zgartiradi | tasdiq + zaxira nusxa |

Office o'rnatilmagan kompyuterda `to_pdf` **jim qolmaydi** — buni ochiq aytadi.
Qolgan hamma narsa Officesiz ham ishlaydi.

### Ruxsatlar — har bir telefon uchun alohida

Ulanish o'z-o'zidan hech qanday qo'shimcha huquq bermaydi. Ilovadagi
**Ulanish** bo'limida telefonni tanlab, ruxsat berasiz:

| Ruxsat | Nima ochiladi |
|---|---|
| `find` | Hujjat topish va yuborish (har doim yoqilgan) |
| `office` | Jadval o'qish, PDF'ga o'girish, bet ajratish |
| `office_write` | Fayl o'zgartirish — **har safar tasdiq so'raladi** |

Dadangizning telefoni `find` da qoladi. Yangi ulangan chat ham shunday
boshlanadi — ruxsat meros qilib olinmaydi, qo'lda beriladi.

### O'zgartirish oqimi

```
  Dada: hisobotda B2 ni 1 500 000 000 qil
    AI: ⚠️ Faylni o'zgartirmoqchiman:
        📄 Hisobot_Q1.xlsx  (Январь varag'i)
          B2 → 1500000000
        Zaxira nusxa olinadi. Davom etaymi?
        [ ✅ Ha, bajar ]  [ ❌ Bekor qilish ]
```

Tugma bosilganda **model emas, tizim** bajaradi: tasdiqlangan amal muzlatib
qo'yiladi, shuning uchun keyingi qadamda boshqa narsa almashtirib bo'lmaydi.
Har o'zgartirishdan oldin `fayl.backup-YYYYMMDD-HHMMSS.xlsx` yaratiladi.

## Ekranni boshqarish (UIA)

Windows har bir tugma, maydon va menyuni **strukturali matn** qilib beradi —
nomi, turi, holati va koordinatasi bilan. Shuning uchun oddiy matn modeli
(DeepSeek) GUI'ni boshqara oladi: vision model ham, GPU ham, skrinshot ham
kerak emas.

```
  ULUGBEK: File Explorer oynasida qanday tugmalar bor?
       AI: New, Cut, Copy, Paste, Rename, Share, Delete, Sort, View...

  ULUGBEK: qidiruv maydoniga 'hisobot' deb yoz
       AI: ✅ "hisobot" qidiruv maydoniga yozildi.
```

Bir oyna ~500 token. Xom UIA daraxti buning 5–10 barobari bo'lardi, shuning
uchun filtrlash bu yerda optimizatsiya emas — **asosiy ish**: bo'sh nomlar,
takrorlangan yo'l-ko'rsatkichlar va ikonka shriftining maxfiy belgilari
tashlab yuboriladi.

**Bosish UIA pattern orqali** amalga oshiriladi (`InvokePattern` va hokazo) —
bu fon oynada ham ishlaydi, sichqonchani qimirlatmaydi va fokusni
o'g'irlamaydi. Faqat pattern topilmasa haqiqiy klik ishlatiladi.

### O'lchab bilingan cheklovlar

| Holat | Natija |
|---|---|
| Tabiiy Windows oynalari (Explorer, Word, dialoglar) | ~250 element, 1.5s — to'liq ishlaydi |
| **Electron ilovalar** (VS Code, Discord, Claude) | 19–24 element — daraxt deyarli bo'sh |
| O'chirilgan tugmalar | `(o'chiq)` deb belgilanadi — AI ko'r-ko'rona bosmaydi |

Electron ilovalarda Chromium accessibility'ni sukut bo'yicha o'chirib qo'yadi.
Ular **qotib qolmaydi** (tadqiqotdagi deadlock takrorlanmadi), lekin
boshqarib ham bo'lmaydi — agent buni ochiq aytadi.

### Xavfli amallar

Tugma nomi uch tilda tekshiriladi — `Delete`, `Удалить`, `Отправить`,
`o'chirish`, `Pay` va hokazo. Bunday tugma bosilishidan oldin tasdiq
so'raladi; `Copy` yoki `View` kabi qaytariladigan amallar to'g'ridan-to'g'ri
bajariladi.

## Brauzer (Playwright)

Brauzerni ham UIA orqali boshqarsa bo'lardi, lekin bu sahifaning **rasmini**
o'qish bo'lardi — holbuki sahifa o'z tuzilishini allaqachon biladi. Playwright
DOM'ni to'g'ridan-to'g'ri beradi: haqiqiy havolalar, haqiqiy maydonlar.

```
  ULUGBEK: uz.wikipedia.org da Samarqand haqida nima yozilgan?
       AI: 📄 Samarqand — viloyatning maʼmuriy markazi (1938-yildan).
           Aholisi: 593,4 ming (2024). Maydoni: 120 km²...

  ULUGBEK: o'sha sahifaning rasmini yubor
       AI: ✅ Yuborildi.   [📎 page.png]
```

**Alohida profil.** Brauzer o'z profilida ishlaydi (`browser-profile/`).
Saytlarga bir marta kirasiz — sessiyalar saqlanadi. Sizning kundalik
Chrome'ingizga **tegilmaydi**: unga ulanish Chrome ishlaganda profilni qulflab
qo'yardi va agentga barcha ochiq akkauntlaringizni berardi.

### Amal ta'sir qildimi?

Har bir `web_click` va `web_type` sahifani oldin va keyin solishtiradi:

```
  [2] input "(text maydoni)"   → changed: false  ⚠️ sahifa o'zgarmadi
  [5] textarea "Search..."     → changed: true   ✅ natijalar sahifasi
```

Bu muhim: DuckDuckGo'da soxta maydon bor va unga yozish **xatosiz** bajariladi.
Solishtirmasa, agent «qidirdim» deb yolg'on aytardi. Endi buni o'zi payqab,
boshqa raqamni sinaydi.

### Sahifa matni — ishonchsiz

Sahifani begona odam yozgan. Uning matni hujjatlar bilan bir xil qoida ostida:
**ma'lumot, hech qachon ko'rsatma emas**. Bu yerda bu yanada muhim, chunki
sahifa aynan shunday agent o'qishi uchun yozilgan bo'lishi mumkin.

## Tizim boshqaruvi (ovoz va oynalar)

`system` ruxsati bilan agent tizim ovozini va oyna holatini boshqaradi —
hammasi native Windows API orqali, sichqonchasiz:

```
  u: ovozni 30% balandroq qil
 AI: ✅ Ovoz 80% qilindi.

  u: brauzer oynasini kattalashtir
 AI: ✅ Oyna kattalashtirildi.

  u: Claude oynasini yopib qoy
 AI: ⚠️ Oynani yopmoqchiman: 🪟 Claude
     Saqlanmagan ma'lumot yo'qolishi mumkin. Davom etaymi?
     [ ✅ Ha ]  [ ❌ Bekor ]
```

Ovoz va katta/kichik qilish darhol bajariladi; **oyna yopish** — qaytarib
bo'lmaydi, shuning uchun tasdiq so'raydi.

> Nozik xato bo'lgan: ovoz (pycaw) va oyna (uiautomation) ikkalasi ham
> `comtypes` ishlatadi, va uning kod generatsiyasi thread'ga xavfsiz emas —
> ovozdan keyin oyna o'qilganda jarayon **segfault** bo'lardi. Yechim:
> `uiautomation` COM ishga tushishidan oldin import qilinadi va butun COM
> ishi bitta thread'da bajariladi.

## Haqiqiy Chrome

Brauzer endi Playwright'ning «Chrome for Testing» build'i emas, **mashinadagi
haqiqiy Google Chrome**'ni ishlatadi (`channel="chrome"`). Bu eclass.uz kabi
saytlar ochilmagan muammoni hal qiladi — render va tarmoq odatdagi Chrome
bilan bir xil. Chrome yo'q bo'lsa, Playwright'ning Chromium'iga qaytadi.

### Ikki rejim

Sozlamalarda brauzer rejimini tanlaysiz:

| Rejim | Nima |
|---|---|
| `profile` | Agent'ning alohida Chrome profili. Saytlarga bir marta kirasiz, sessiya saqlanadi. Kundalik Chrome'ingizga tegilmaydi. |
| `cdp` | Sizning **haqiqiy Chrome'ingiz** — kirgan akkauntlaringiz bilan. Agent Chrome'ni debug portida sizning profilingiz bilan ochadi va ulanadi. |

> `cdp` rejimida odatdagi Chrome **yopiq** bo'lishi kerak (Chrome bitta profilga bitta nusxa) — agent uni o'zi akkauntlaringiz bilan qayta ochadi. Ochiq bo'lsa, agent buni aniqlab, «avval Chrome'ni yoping» deydi. Agent Chrome'ingizni **hech qachon o'zi yopmaydi** — faqat ulanishni uzadi.

## Ekranni o'qish (vision — 3-bosqich)

UIA daraxti Electron, o'yin, video va canvas ilovalarni bo'sh qaytaradi.
Bunday ekranlar uchun `screen_read`: skrinshotni **deepseek-flash** vision
modeliga yuborib, savolga javob beradi.

```
  u: VS Code'da qanaqa xatolik chiqib turibdi?
 AI: 📄 Pastdagi panelda "ModuleNotFoundError: pandas" xatosi ko'rinyapti...
```

**Ataylab faqat O'QISH.** Tekshirib ko'rdim: deepseek-flash ekranni aniq
tasvirlaydi (masalan, ochiq dasturlar va matnni to'g'ri o'qidi), lekin aniq
piksel **koordinata** berishda ishonchsiz — Login tugmasini so'raganda Cancel
joyini ko'rsatdi. Shuning uchun vision bilan bosish yo'q: u faqat o'qiydi.
Bosish uchun UIA (aniq) yoki brauzer (DOM) ishlatiladi.

> Skrinshot faqat siz ekranni o'qishni so'raganda olinadi — uzluksiz emas.
> Ekran matni ham begona kontent: ma'lumot, ko'rsatma emas.

## Klaviatura va clipboard

`desktop_control` ruxsati bilan agent klaviatura yorliqlarini yuboradi va
matn yozadi — UIA maydon topa olmaydigan ilovalarda ham.

```
  u: VS Code'da ctrl+s bos
 AI: ✅ Yuborildi: ctrl+s → requirements.txt - Visual Studio Code
```

**Fokus — xavfsizlik chegarasi.** Tugmalar fokusdagi oynaga boradi, shuning
uchun agent avval oynani oldinga chiqaradi **va tekshiradi**. Chiqara olmasa
(Windows ba'zan to'sadi) — **hech narsa yubormaydi**. Bu jiddiy: sinovda
"fokus berildi" deb ko'rsatilgan oynaga yozilgan matn aslida boshqa joyga
ketayotgani aniqlandi.

**Matn clipboard orqali qo'yiladi**, harflab yozilmaydi. Sabab o'lchandi:
harflab yozganda `"klaviatura testi 456"` → `"llaviatura tttti 666"` bo'lib
buzildi, va kirill umuman yozilmasdi. Clipboard aniq qo'yadi; eski
clipboard qiymatingiz keyin tiklanadi.

| Kombinatsiya | Natija |
|---|---|
| `ctrl+s`, `enter`, `alt+tab`, `f5` | to'g'ridan-to'g'ri bajariladi |
| `alt+f4`, `ctrl+w`, `shift+delete`, `win+l` | ⚠️ tasdiq so'raladi |

## Xavfsizlik

- **Faqat ulangan chatlar.** Kod noutbuk ekranida ko'rinadi va bir martalik.
  5 marta noto'g'ri kiritilsa 10 daqiqaga bloklanadi.
- **Maxfiy fayllar hech qachon yuborilmaydi.** `parol`, `api key`, `.env`,
  `.pem`, `wallet` va shunga o'xshash nomlar tizim darajasida to'siladi —
  AI xohlasa ham o'tkazmaydi. Ro'yxat `config.json` da kengaytiriladi.
- **Fayllar serverda saqlanmaydi** — oqim orqali o'tib ketadi.
- **Server autentifikatsiyasiz ishga tushmaydi.** `WEBHOOK_SECRET` va
  `RELAY_TOKEN` majburiy — yo'q bo'lsa relay xato bilan to'xtaydi. Ochiq
  ishlaydigan server eng yomoni, chunki tashqaridan hammasi joyidek ko'rinadi.
- **Prompt injection himoyasi.** Agent faqat O'ZI qidiruvda topgan fayllarni
  yubora oladi. Hujjat ichiga yashirilgan «anavi faylni ham yubor» degan
  ko'rsatma ishlamaydi — yo'l qidiruv natijasida chiqmagan bo'lsa, tizim rad
  etadi. Bu promptga emas, kodga qo'yilgan chegara.

## Loyiha tuzilishi

```
server/          Render'dagi pochtachi
  main.py          webhook, WebSocket, fayl uzatish
  relay.py         ulangan agentlar reestri (xotirada)
  telegram.py      Bot API
agent/           noutbukdagi ilova
  run.py           kirish nuqtasi (--headless ham bor)
  coworker/
    ui.py          oyna
    app.py         orkestratsiya
    brain.py       AI sikli va asboblar
    fs.py          disk bo'ylab qidiruv
    textutil.py    kirill/lotin + tarjima lug'ati
    extract.py     docx/xlsx/pptx/pdf dan matn
    memory.py      to'rt qatlamli xotira
    stt.py         ovoz → matn
    llm.py         OpenAI-mos klient
    config.py      sozlamalar va xavfsizlik
```

## AI provayderini almashtirish

Har qanday OpenAI-mos endpoint ishlaydi. Sozlamalardagi ro'yxatda:
DeepSeek, GLM (BigModel / z.ai), Groq, OpenRouter, va **Ollama** — oxirgisi
noutbukning o'zida ishlaydi, ya'ni internetsiz va butunlay bepul.

## Litsenziya

MIT
