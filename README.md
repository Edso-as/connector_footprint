# 連接器 PDF → KiCad Footprint 工具

從連接器規格書 PDF 的「建議焊盤圖 (Recommended PCB layout)」量測焊盤位置，產生 KiCad `.kicad_mod` 與 `.csv`，並用圖上標註的尺寸驗證結果。

> 目前只在一份規格書（`DA2-4164R-60BMX-U.pdf`，3.5 mm 音源插座）上測試過。其他連接器請先試跑並自行核對。

## 內容

| 檔案 | 用途 |
|---|---|
| `footprint_wizard.py` | 圖形介面（六步驟精靈） |
| `.github/skills/pdf-to-footprint/` | Copilot skill：由 LLM 讀圖，腳本量測並驗證 |
| `.github/skills/pdf-to-footprint/scripts/measure.py` | 命令列量測與建置腳本（skill 使用） |
| `extract.py` | 早期的單檔範例腳本，只對示範 PDF 有效 |
| `DA2-4164R-60BMX-U.kicad_mod` / `.csv` | 示範輸出 |
| `DA2-4164R-60BMX-U.extracted.kicad_mod` | `extract.py` 產生、只含焊盤 |
| `docs/使用手冊.md` | 詳細操作說明 |

## 安裝

需要 Python 3.10 以上。

```
pip install pymupdf opencv-python-headless numpy
```

GUI 使用 Python 內建的 Tkinter，不需另外安裝。

## 快速開始

**圖形介面**
```
python footprint_wizard.py
```

**命令列（範例）**
```
python .github\skills\pdf-to-footprint\scripts\measure.py find DA2-4164R-60BMX-U.pdf
python .github\skills\pdf-to-footprint\scripts\measure.py detect DA2-4164R-60BMX-U.pdf --region 31,320,255,493 --out out
python .github\skills\pdf-to-footprint\scripts\measure.py build out\spec.json --out out
```

**Copilot skill**：在此資料夾開啟對話，說「幫我把這份 PDF 的建議焊盤圖做成 KiCad footprint」，或輸入 `/pdf-to-footprint`。

詳細步驟請見 [docs/使用手冊.md](docs/使用手冊.md)。

## 輸出

- `<name>.kicad_mod`：KiCad 8 格式，單位 mm，Y 軸向下。
- `<name>.csv`：`pin,x_mm,y_mm,pad_w_mm,pad_h_mm,drill_w_mm,drill_h_mm`。

## 限制

- 只輸出焊盤與一個依焊盤外緣加 0.25 mm 的 courtyard 框。本體外框、絲印、pin 1 標記、定位柱 (NPTH)、PCB 缺口都不會產生。
- 自動找區域與偵測焊盤依賴「有斜線陰影的橢圓焊盤」。實心 SMD 焊盤等其他畫法可能找不到，需要手動框選。
- 沒有 OCR：標註數字（距離、焊盤與鑽孔尺寸）由使用者或 LLM 讀圖後輸入。
- 驗證容許誤差為 0.1 mm，請和圖面公差（例如 ±0.05）比較。
- 輸出檔未在 KiCad 內開啟驗證，請用 Footprint Editor 開啟並和 PDF 對照。
- 3D 模型不在範圍內。
