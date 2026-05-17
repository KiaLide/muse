# 🎵 Muse — 個人音樂品味分析系統

> 建立屬於你自己的音樂「DNA」：把你喜歡的歌加進來，Muse 會分析音訊特徵、找出你的偏好規律，並根據你的品味主動推薦新歌。

---

## 目錄

1. [功能簡介](#功能簡介)
2. [系統需求](#系統需求)
3. [快速開始](#快速開始)
4. [使用說明](#使用說明)
5. [運作原理](#運作原理)
6. [技術架構](#技術架構)
7. [常見問題](#常見問題)

---

## 功能簡介

| 功能 | 說明 |
|------|------|
| 📥 加入歌曲 | 貼上 YouTube 或 Spotify 連結，自動下載並分析音訊 |
| 🎤 上傳音檔 | 支援上傳本地 MP3 / WAV / FLAC 等格式 |
| 📊 分析品味 | 從你的歌單提取音樂特徵向量，建立個人偏好剖面 |
| 🔍 推薦歌曲 | 貼上任意 YouTube 連結，Muse 從中找出最符合你品味的歌 |
| ⭐ 評分歌曲 | 貼上連結，Muse 告訴你這首歌和你的品味有多相符 |
| 🗂️ 多設定檔 | 最多 3 個獨立設定檔，可分開管理不同風格的歌單 |

---

## 系統需求

- **作業系統**：Windows 10 / 11（macOS / Linux 需手動安裝依賴）
- **Python**：3.10 或更新版本
- **FFmpeg**：用於音訊下載與轉換
- **網路連線**：用於下載音訊與搜尋 YouTube

> **Windows 使用者免煩惱**：`啟動.bat` 會自動偵測並安裝 Python 和 FFmpeg。

---

## 快速開始

### Windows（推薦）

1. [下載此專案](https://github.com/YOUR_USERNAME/muse/archive/refs/heads/main.zip) 並解壓縮
2. 雙擊執行 **`啟動.bat`**
3. 等待自動安裝依賴套件（首次約需 2–5 分鐘）
4. 瀏覽器會自動開啟 `http://127.0.0.1:5000`

### 手動啟動（macOS / Linux）

```bash
# 1. 安裝 FFmpeg
# macOS: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg

# 2. 安裝 Python 套件
pip install -r requirements.txt

# 3. 啟動伺服器
python server.py
```

然後開啟瀏覽器前往 `http://127.0.0.1:5000`

---

## 使用說明

### 第一步：建立你的歌單

前往「**加入歌曲**」分頁，貼上你喜歡的歌曲連結：

- YouTube 單曲連結：`https://www.youtube.com/watch?v=...`
- YouTube 播放清單：`https://www.youtube.com/playlist?list=...`
- Spotify 單曲連結：`https://open.spotify.com/track/...`
- Spotify 播放清單：`https://open.spotify.com/playlist/...`

> 建議至少加入 **10 首以上**，分析結果會更準確。

### 第二步：分析你的音樂品味

切換到「**分析品味**」分頁，點擊「開始分析」。系統會計算你歌單的整體音樂特徵，並以 11 個維度呈現你的音樂偏好：

| 維度 | 說明 |
|------|------|
| 節奏速度 | BPM，歌曲的快慢 |
| 音量強度 | 整體音量大小 |
| 動態範圍 | 輕聲與大聲的落差幅度 |
| 諧波豐富度 | 音色的和諧程度（人聲、弦樂偏高） |
| 打擊感 | 鼓聲、節拍的比例 |
| 音色明亮度 | 高頻比例（電吉他、電子樂偏高） |
| 起伏密度 | 音符出現的頻率 |
| 節拍規律性 | 節奏的整齊程度 |
| 音色溫暖度 | 低頻豐富程度（bass、大提琴偏高） |
| 音調豐富度 | 和弦複雜程度 |
| 旋律張力 | 旋律線條的起伏幅度 |

### 第三步：探索推薦歌曲

在「**分析品味**」頁面捲動到下方，貼上 YouTube 頻道或播放清單連結，Muse 會掃描其中的影片，篩選出最符合你品味的歌曲，並可選擇以下篩選條件：

- **風格**：電子、搖滾、爵士、古典、民謠、嘻哈 等
- **情緒**：快樂、平靜、充滿活力、悲傷、浪漫 等
- **語言**：中文、日文、韓文、英文
- **年代**：年代範圍篩選
- **人聲性別**：男聲、女聲
- **排除/包含關鍵字**：自訂黑白名單

點擊推薦卡片上的「**詳細分析**」可查看：
- 各維度與你偏好的對比圖
- 為什麼推薦這首歌的說明文字

### 評分歌曲

在「**評分歌曲**」分頁貼入連結，Muse 會給出一個相符度分數（0–100）並附上各維度分析。

### 多設定檔

點擊右上角的設定檔選單可切換至多達 3 個獨立設定檔，每個設定檔有自己的歌單與品味資料，互不干擾。雙擊設定檔名稱可重新命名。

---

## 運作原理

### 音訊分析：83 維特徵向量

每首歌加入後，Muse 使用 [librosa](https://librosa.org/) 對音檔進行深度音訊分析，提取 **83 個數值特徵**，涵蓋：

```
節奏類：  tempo（BPM）、beat_regularity、onset_mean/std
能量類：  rms_mean/std、dynamic_range、harmonic_ratio、percussive_ratio
頻譜類：  spectral_centroid、spectral_bandwidth、spectral_rolloff
          spectral_flatness、zcr（過零率）
音色類：  mfcc_0 ~ mfcc_19（梅爾頻率倒譜係數，共 20 維）
          spectral_contrast_0 ~ 6（頻帶對比，共 7 維）
和聲類：  chroma_0 ~ 11（12 個音階強度）
          tonnetz_0 ~ 5（調性張力向量）
```

分析完成後，音檔立即刪除，只保留這 83 個數字。

### 品味建模：偏好剖面

當你點「分析品味」，系統對歌單中所有歌曲的特徵向量取**逐維度平均值**，得到你的「品味中心點」。同時將 83 維原始特徵映射為 11 個人類可理解的維度（節奏速度、音量強度等），以 0–100 分呈現。

### 推薦機制：餘弦相似度 + 多層篩選

推薦時，Muse 對候選歌曲的特徵向量與你的品味中心點計算**餘弦相似度**：

```
similarity = dot(candidate_vec, profile_vec) / (|candidate_vec| × |profile_vec|)
```

餘弦相似度衡量兩個向量的「方向相似性」，不受音量等絕對數值影響，更能反映音樂風格的本質相似程度。

篩選條件經過多層處理（**在下載音訊前先用 metadata 過濾，減少不必要的下載**）：

```
Layer 1：影片時長過濾（1 分 30 秒 ~ 10 分鐘）
Layer 2：語言 — YouTube 標題語言 / metadata 語言偵測
Layer 3：年代 — metadata 發行年份 / 網路搜尋驗證
Layer 4：風格 — metadata 關鍵字匹配（優先於音訊分析）
Layer 4b：情緒 — metadata 關鍵字匹配（偵測到矛盾情緒直接跳過）
Layer 5：排除關鍵字 — 黑名單 metadata 過濾
Layer 6：下載 120 秒音訊並分析
Layer 7：風格音訊驗證（metadata 與音訊交叉確認）
Layer 8：情緒音訊驗證（metadata 確認則跳過音訊分析）
Layer 9：人聲性別 — F0 基頻估算 + 網路搜尋
Layer 10：包含關鍵字 — 白名單確認
最終：相似度排序，取前 N 名
```

### 評分說明

評分歌曲的分數計算：

1. 將候選歌曲的 11 維人類可讀特徵與你的品味剖面逐維比對
2. 計算每個維度的差距（0–100 分制）
3. **整體相符度 = 100 − 所有維度差距的平均值**

各維度標籤說明：
- 🟢 **匹配**：差距 < 15 分
- 🔴 **偏高**：本曲此維度數值比你的偏好高 15 分以上
- 🔵 **偏低**：本曲此維度數值比你的偏好低 15 分以上

---

## 技術架構

```
muse/
├── server.py        # Flask 後端 API 伺服器
├── muse.html        # 單頁前端應用（純 HTML + CSS + JS，無框架）
├── requirements.txt # Python 依賴套件
├── 啟動.bat         # Windows 一鍵啟動腳本（含自動安裝）
└── muse.db          # SQLite 資料庫（自動建立，不進版本控制）
```

### 後端（server.py）

| 技術 | 用途 |
|------|------|
| Flask | HTTP API 伺服器、Server-Sent Events（SSE）串流進度 |
| yt-dlp | YouTube / 網路影片資訊擷取與音訊下載 |
| librosa | 專業音訊分析（頻譜、節奏、MFCC 等） |
| numpy / scipy | 特徵向量運算、餘弦相似度 |
| SQLite | 本地資料庫儲存歌曲特徵與設定檔 |

### 前端（muse.html）

純 HTML + CSS + JavaScript，無任何前端框架依賴。使用 **EventSource API** 接收伺服器推送的即時進度更新（SSE）。

### API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| `/api/songs` | GET | 取得歌單 |
| `/api/add-song` | GET (SSE) | 新增歌曲（串流進度） |
| `/api/analyze-upload` | POST (SSE) | 上傳音檔分析 |
| `/api/analyze-taste` | GET (SSE) | 分析品味 |
| `/api/recommend-audio` | GET (SSE) | 推薦歌曲 |
| `/api/rate-song` | GET (SSE) | 評分歌曲 |
| `/api/profiles` | GET | 取得設定檔列表 |
| `/api/profiles/:id` | PUT | 重新命名設定檔 |
| `/api/delete-song/:id` | DELETE | 刪除歌曲 |

---

## 常見問題

**Q：分析一首歌要多久？**
A：通常 30–90 秒。系統只下載歌曲的前 2 分鐘進行分析，分析完立即刪除音檔。

**Q：我的歌曲資料存在哪裡？**
A：全部存在本地的 `muse.db`（SQLite 資料庫），只有音訊特徵數值，不儲存音樂本身。

**Q：推薦時掃描一個 YouTube 頻道要多久？**
A：每首候選歌曲約 30–90 秒（需下載分析）。建議先用篩選條件縮小範圍，或設定較小的候選數量。

**Q：Spotify 連結支援哪些？**
A：Spotify 單曲與播放清單。系統會用歌曲名稱和歌手在 YouTube 上搜尋並下載音訊進行分析（不直接下載 Spotify 音訊）。

**Q：能在 Mac 或 Linux 上使用嗎？**
A：可以，但需要手動安裝 FFmpeg 和 Python 套件（參見上方手動安裝說明）。

**Q：資料會上傳到網路嗎？**
A：不會。所有分析都在你的電腦本地執行，只有以下情況會連線：
- 從 YouTube 下載音訊
- 搜尋 YouTube 候選歌曲
- 取得 Spotify 歌曲資訊（oEmbed API）

---

## License

MIT License — 自由使用、修改、分發。
