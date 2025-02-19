# 🎬 Vidium – High-Performance Media Converter & Downloader

Vidium is a **GPU-accelerated media converter and downloader** built with **PySide6** and **FFmpeg**. Designed for **Windows 11**, it supports a wide range of video and audio formats, trimming, batch processing, and downloading.

---

## 🚀 Features

### 🎥 **Conversion & Trimming**
- **Full Format Support**: Convert between **MP4, MKV, WebM, MP3, WAV, GIF**, and more.
- **GPU Acceleration**: Utilize **NVIDIA NVENC** for fast conversions.
- **High-Speed WebM Encoding**: Optimized VP9 settings for efficient CPU-based conversions.
- **Trimming**: Accurately trim videos without re-encoding (stream copy) or with re-encoding for precise edits.
- **Trim & Convert**: Seamlessly trim videos before converting them to your desired format.
- **GIF Creation**: Generate high-quality GIFs with customizable FPS (default **12 FPS** for balance).

### 📥 **Media Downloader**
- **Platform Integration**: Download videos from **YouTube, Reddit, Twitter**, and more via **yt-dlp**.
- **Download & Convert**: Instantly convert downloaded media to your preferred format.
- **Download & Trim**: Cut out specific sections of videos directly after downloading.

### ⚡ **Performance-Oriented**
- **Asynchronous Processing**: Non-blocking UI ensures responsive performance during heavy conversions.
- **Batch Processing**: Queue multiple files for conversion or download.
- **Drag & Drop**: Quickly add files by simply dragging them into the app.
- **Optimized FFmpeg Integration**: Fully bundled FFmpeg with pre-configured hardware acceleration.

### 🎨 **User-Friendly Interface**
- **Preview Panel**: Live preview videos before and after conversion.
- **Customizable Output**: Adjust quality, select output folders, and set default preferences.
- **Progress Tracking**: Real-time progress bars for individual files and overall batches.
- **Console Log**: Detailed logs for every conversion and download process.

---

## 📁 Supported Formats

| **Input Formats** | **Output Formats**        |
|------------------|---------------------------|
| `.mp4`, `.mkv`, `.webm` | `.mp4`, `.mkv`, `.webm`, `.mp3`, `.wav`, `.gif` |

---

## 🛠️ Installation

### **🔗 Prerequisites**
- **Windows 11**
- **Python 3.9+**
- **FFmpeg** *(Bundled, no need for manual installation)*
- **yt-dlp** *(Bundled)*

### **📦 Clone & Install**
```bash
git clone https://github.com/your-username/vidium.git
cd vidium
pip install -r requirements.txt
