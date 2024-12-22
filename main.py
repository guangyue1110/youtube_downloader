from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse
from pathlib import Path
import yt_dlp
import json
import os
import asyncio
import time
import logging
import sys
import requests
from urllib.parse import urlparse
import subprocess
from urllib3.contrib.socks import SOCKSProxyManager
import urllib3
import random
from typing import List

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI()

# 配置模板和静态文件
templates = Jinja2Templates(directory="templates")
app.mount("/downloads", StaticFiles(directory="downloads"), name="downloads")
app.mount("/static", StaticFiles(directory="static"), name="static")

# 配置下载目录
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# 存储下载任务信息
DOWNLOADS_FILE = "downloads.json"
active_downloads = {}

# 修改代理配置常量
PROXY_URL = "socks5://127.0.0.1:41287"  # 更新为新的SOCKS5代理端口

# 添加User-Agent池
USER_AGENTS = [
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
]

# 添加请求头生成函数
def get_random_headers() -> dict:
    return {
        'User-Agent': random.choice(USER_AGENTS),
        'Accept': '*/*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://www.youtube.com',
        'Referer': 'https://www.youtube.com/',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-origin',
        'Connection': 'keep-alive',
        'DNT': '1',
        'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"macOS"',
    }

def load_downloads():
    if os.path.exists(DOWNLOADS_FILE):
        with open(DOWNLOADS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_downloads(downloads):
    with open(DOWNLOADS_FILE, 'w', encoding='utf-8') as f:
        json.dump(downloads, f, ensure_ascii=False, indent=2)

class DownloadProgress:
    def __init__(self):
        self.progress = 0
        self.status = "pending"
        self.info = {}
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.speed = 0
        self.eta = 0
    
    def hook(self, d):
        if d['status'] == 'downloading':
            self.downloaded_bytes = d.get('downloaded_bytes', 0)
            self.total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            self.speed = d.get('speed', 0)
            self.eta = d.get('eta', 0)
            
            if self.total_bytes > 0:
                self.progress = (self.downloaded_bytes / self.total_bytes) * 100
                
        elif d['status'] == 'finished':
            self.status = "completed"
            self.progress = 100

async def verify_proxy():
    try:
        # 使用yt-dlp测试代理
        cmd = [
            sys.executable,  # 使用当前Python解释器
            '-m',
            'yt_dlp',
            '--proxy', 'socks5://127.0.0.1:41287',  # 更新代理端口
            '--simulate',
            '--verbose',
            '--no-warnings',
            'https://www.youtube.com/watch?v=jNQXAC9IVRw'
        ]
        
        logger.info(f"Running command: {' '.join(cmd)}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env={**os.environ, 'PYTHONUNBUFFERED': '1'}
        )
        
        if result.returncode == 0:
            logger.info("YouTube proxy test successful")
            return True
        
        logger.error(f"Proxy test failed with code {result.returncode}")
        logger.error(f"Error output: {result.stderr}")
        logger.error(f"Standard output: {result.stdout}")
        return False
        
    except Exception as e:
        logger.error(f"Proxy verification failed: {str(e)}")
        logger.exception("Detailed error:")
        return False

async def download_video(url: str, task_id: str):
    progress = DownloadProgress()
    active_downloads[task_id] = progress
    
    try:
        # 添加随机延迟
        await asyncio.sleep(random.uniform(1, 3))
        
        # 验证代理
        logger.info(f"Verifying proxy: {PROXY_URL}")
        retry_count = 3
        while retry_count > 0:
            if await verify_proxy():
                break
            retry_count -= 1
            if retry_count > 0:
                # 随机化重试间隔
                await asyncio.sleep(random.uniform(2, 5))
                logger.info(f"Retrying proxy verification, {retry_count} attempts left")
        
        if retry_count == 0:
            raise Exception("代理服务器连接失败，请检查代理设置")

        # 配置yt-dlp选项
        ydl_opts = {
            'format': 'best[height<=720]',
            'outtmpl': str(DOWNLOAD_DIR / '%(title).100s-%(id)s.%(ext)s'),
            'progress_hooks': [progress.hook],
            
            # 代理设置
            'proxy': PROXY_URL,
            
            # 网络设置
            'socket_timeout': 60,
            'retries': 10,
            'fragment_retries': 10,
            'retry_sleep': lambda n: random.uniform(1, 3) * (2 ** (n - 1)),  # 指数退避重试
            'buffersize': 1024 * 1024,
            'concurrent_fragment_downloads': 1,
            
            # 添加随机请求头
            'http_headers': get_random_headers(),
            
            # YouTube相关设置
            'nocheckcertificate': True,
            'extractor_retries': 5,
            
            # 调试选项
            'verbose': True,
            'debug_printtraffic': True,
            
            # 添加更多反爬设置
            'sleep_interval': random.uniform(1, 3),  # 请求间隔
            'max_sleep_interval': 5,
            'sleep_interval_requests': 1,  # 每次请求后休息
            
            # 错误处理
            'ignoreerrors': False,
            'no_warnings': False,
            'extract_flat': False,
        }
        
        # 更新 yt-dlp
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp"], 
                         check=True, capture_output=True)
            logger.info("yt-dlp has been updated to the latest version")
        except Exception as e:
            logger.warning(f"Failed to update yt-dlp: {e}")

        # 添加延迟
        await asyncio.sleep(2)
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                # 先尝试获取基本信息
                logger.info(f"Extracting info for URL: {url}")
                basic_info = await asyncio.to_thread(
                    ydl.extract_info, url, download=False, process=False
                )
                
                if not basic_info:
                    raise Exception("无法获取视频信息")
                
                # 获取完整信息并下载
                info = await asyncio.to_thread(
                    ydl.extract_info, url, download=True
                )
                
                if not info:
                    raise Exception("视频下载失败")
                
                # 修改文件路径存储方式
                video_id = info.get('id', '')
                video_title = info.get('title', 'Unknown Title')
                video_ext = info.get('ext', 'mp4')
                filename = f"{video_title}-{video_id}.{video_ext}"
                
                # 保存视频信息
                progress.info = {
                    'title': video_title,
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', 'Unknown Uploader'),
                    'description': info.get('description', 'No description'),
                    'thumbnail': info.get('thumbnail', ''),
                    'filepath': filename,  # 只存储文件名
                    'video_id': video_id
                }
                
                # 保存下载记录
                downloads = load_downloads()
                downloads.append(progress.info)
                save_downloads(downloads)
                
            except Exception as e:
                logger.error(f"Error during download: {str(e)}")
                raise
                
    except Exception as e:
        error_msg = f"Download failed: {str(e)}"
        logger.error(error_msg)
        progress.status = "error"
        progress.error = error_msg
    
    finally:
        # 清理环境变量
        for var in ['ALL_PROXY', 'HTTPS_PROXY', 'HTTP_PROXY', 'SOCKS_PROXY']:
            os.environ.pop(var, None)
        
        # 5分钟后清理任务记录
        await asyncio.sleep(300)
        if task_id in active_downloads:
            del active_downloads[task_id]

@app.get("/")
async def home(request: Request):
    downloads = load_downloads()
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "downloads": downloads}
    )

# 添加请求限制装饰器
def rate_limit(max_per_minute: int = 30):
    def decorator(func):
        last_requests = []
        
        async def wrapper(*args, **kwargs):
            now = time.time()
            # 清理过期请求记录
            while last_requests and now - last_requests[0] > 60:
                last_requests.pop(0)
            
            # 检查是否超过限制
            if len(last_requests) >= max_per_minute:
                wait_time = 60 - (now - last_requests[0])
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
            
            last_requests.append(now)
            return await func(*args, **kwargs)
        return wrapper
    return decorator

# 应用请求限制
@app.post("/download")
@rate_limit(max_per_minute=30)
async def start_download(url: str, background_tasks: BackgroundTasks):
    task_id = str(time.time())
    background_tasks.add_task(download_video, url, task_id)
    return {"task_id": task_id}

@app.get("/progress/{task_id}")
async def get_progress(task_id: str):
    if task_id not in active_downloads:
        return JSONResponse({"status": "not_found"})
    
    progress = active_downloads[task_id]
    return {
        "status": progress.status,
        "progress": progress.progress,
        "info": progress.info,
        "error": getattr(progress, 'error', None),
        "downloaded_bytes": progress.downloaded_bytes,
        "total_bytes": progress.total_bytes,
        "speed": progress.speed,
        "eta": progress.eta
    }
