#!/usr/bin/env python3
"""
Complete Speech Monitor with Fixed Mode Switching
- UNMUTE: real-time pass-through (FIXED - no more audio loss)
- MUTE: PartialTranscript -> regex muting (minimal delay) + clean audio passes through
- FinalTranscript -> Tisane reporting for comprehensive child safety

Fixed issues:
- MUTE mode properly plays clean audio
- UNMUTE mode audio restored properly after mode switch
- Better audio stream management
- Enhanced debugging for troubleshooting
"""

import asyncio
import base64
import json
import math
import struct
import subprocess
import threading
import os
from collections import deque
from datetime import datetime
from time import sleep, time
import re

import pyaudio
import RPi.GPIO as GPIO
import websockets
#from configure import auth_key
from configure import auth_key, TISANE_SCRIPT

# =========================
# CONFIG - CORRECTED PATHS
# =========================
RATE = 8000
CHANNELS = 1
FORMAT = pyaudio.paInt16
FRAMES_PER_BUFFER = 800   # ~0.1s per frame

BUFFER_SECONDS = 4.0      # buffer for look-ahead
BUFFER_FRAMES = int((RATE * BUFFER_SECONDS) // FRAMES_PER_BUFFER)

REPLACE_MODE = "beep"
BEEP_FREQ_HZ = 1000
BEEP_VOL = 0.3

RED_PIN, GREEN_PIN, BLUE_PIN = 12, 19, 13
BUTTON_PIN = 11

# Corrected paths to match your directory structure
#TISANE_SCRIPT = "/home/pi/sr/tesani_modified.sh"
REPORT_FILE = "/home/pi/sr/report.log"
BAD_FILE = "/home/pi/sr/bad.txt"

# =========================
# LOAD BAD WORDS FROM FILE
# =========================
def load_bad_words():
    """Load bad words with proper error handling"""
    if not os.path.exists(BAD_FILE):
        print(f"Warning: Bad words file {BAD_FILE} not found. Using comprehensive built-in filter.")
        # Create comprehensive built-in patterns for immediate muting
        patterns = [
            # Profanity
            r'\bf+u+c+k+\b', r'\bs+h+i+t+\b', r'\bd+a+m+n+\b', r'\bh+e+l+l+\b',
            r'\bb+i+t+c+h+\b', r'\ba+s+s+\b', r'\bc+r+a+p+\b',
            
            # Sexual content
            r'\bs+e+x+\b', r'\bp+o+r+n+\b', r'\bn+a+k+e+d+\b', r'\bn+u+d+e+\b',
            
            # Violence
            r'\bk+i+l+l+\b', r'\bm+u+r+d+e+r+\b', r'\bg+u+n+\b', r'\bk+n+i+f+e+\b',
            
            # Drugs
            r'\bw+e+e+d+\b', r'\bd+r+u+g+s+\b', r'\bc+o+k+e+\b',
            
            # Hate speech
            r'\bn+i+g+g+e+r+\b', r'\bf+a+g+g+o+t+\b',
            
            # Insults
            r'\bs+t+u+p+i+d+\b', r'\bi+d+i+o+t+\b', r'\bm+o+r+o+n+\b'
        ]
        return re.compile('|'.join(patterns), re.IGNORECASE)

    try:
        with open(BAD_FILE, "r") as f:
            lines = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not lines:
            print("Warning: Bad words file is empty, using built-in comprehensive filter")
            return load_bad_words()  # Use built-in if file is empty
        
        print(f"Loaded {len(lines)} harmful content patterns for real-time muting")
        return re.compile("|".join(f"(?:{line})" for line in lines), re.IGNORECASE)
    except Exception as e:
        print(f"Error loading bad words file: {e}, using built-in filter")
        return load_bad_words()  # Use built-in on error

BAD_REGEX = load_bad_words()

# =========================
# GPIO
# =========================
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BOARD)
for pin in (RED_PIN, GREEN_PIN, BLUE_PIN):
    GPIO.setup(pin, GPIO.OUT)
GPIO.setup(BUTTON_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)

def led_off(): GPIO.output([RED_PIN, GREEN_PIN, BLUE_PIN], [1, 1, 1])
def led_red(): GPIO.output([RED_PIN, GREEN_PIN, BLUE_PIN], [0, 1, 1])
def led_green(): GPIO.output([RED_PIN, GREEN_PIN, BLUE_PIN], [1, 1, 0])
def led_blue(): GPIO.output([RED_PIN, GREEN_PIN, BLUE_PIN], [1, 0, 1])

led_off()
led_blue()

MODE_UNMUTE = "UNMUTE"
MODE_MUTE = "MUTE"
current_mode = MODE_UNMUTE
mode_lock = threading.Lock()

def ts(): return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# =========================
# AUDIO
# =========================
p = pyaudio.PyAudio()
BYTES_PER_SAMPLE = 2
SILENCE_FRAME = b"\x00" * (FRAMES_PER_BUFFER * BYTES_PER_SAMPLE)

def gen_beep_frame():
    samples = []
    for n in range(FRAMES_PER_BUFFER):
        t = n / RATE
        s = int(max(-1.0, min(1.0, BEEP_VOL * math.sin(2*math.pi*BEEP_FREQ_HZ*t)))*32767)
        samples.append(s)
    return struct.pack("<" + "h"*FRAMES_PER_BUFFER, *samples)

BEEP_FRAME = gen_beep_frame()
def replacement_frame(): return BEEP_FRAME if REPLACE_MODE.lower()=="beep" else SILENCE_FRAME

try:
    in_stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                       input=True, frames_per_buffer=FRAMES_PER_BUFFER)
    out_stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE,
                        output=True, frames_per_buffer=FRAMES_PER_BUFFER)
    print("Audio streams initialized successfully")
except Exception as e:
    print(f"Error initializing audio streams: {e}")
    print("Make sure your Pi has audio devices configured")
    exit(1)

# =========================
# BUFFER & DELAY QUEUE - FIXED
# =========================
audio_buffer = deque(maxlen=BUFFER_FRAMES)
muted_indices = {}  # Changed to dict with timestamps for cleanup
audio_lock = threading.Lock()
muted_lock = threading.Lock()
frame_index = 0

# FIXED: More conservative delay calculation
DELAY_FRAMES = max(20, int(2.0 / (FRAMES_PER_BUFFER / RATE)))  # At least 20 frames, ~2s delay
delay_queue = deque(maxlen=DELAY_FRAMES + 10)  # Slightly larger queue

# Cleanup muted indices older than 10 seconds
MUTED_CLEANUP_INTERVAL = 10.0

print(f"Audio configuration:")
print(f"  Sample rate: {RATE} Hz")
print(f"  Buffer frames: {BUFFER_FRAMES}")
print(f"  Delay frames: {DELAY_FRAMES} (~{DELAY_FRAMES * FRAMES_PER_BUFFER / RATE:.1f}s)")

def cleanup_muted_indices():
    """Remove old muted indices to prevent memory leak"""
    cutoff_time = time() - MUTED_CLEANUP_INTERVAL
    with muted_lock:
        to_remove = [idx for idx, timestamp in muted_indices.items() if timestamp < cutoff_time]
        for idx in to_remove:
            del muted_indices[idx]
    if to_remove:
        print(ts(), f"[Cleanup] Removed {len(to_remove)} old muted indices")

def mark_frames_for_muting(frame_ids):
    current_time = time()
    with muted_lock:
        for idx in frame_ids:
            muted_indices[idx] = current_time
    print(ts(), f"[SelectiveMute] Marked {len(frame_ids)} frames for muting.")

# =========================
# AUDIO STREAM MANAGEMENT - NEW
# =========================
def check_audio_streams():
    """Check if audio streams are active and working"""
    try:
        in_active = in_stream.is_active() if hasattr(in_stream, 'is_active') else True
        out_active = out_stream.is_active() if hasattr(out_stream, 'is_active') else True
        print(ts(), f"Audio streams - Input: {'Active' if in_active else 'Inactive'}, Output: {'Active' if out_active else 'Inactive'}")
        
        # Try to restart streams if they're not active
        if not in_active:
            print(ts(), "Restarting input stream...")
            in_stream.stop_stream()
            in_stream.start_stream()
            
        if not out_active:
            print(ts(), "Restarting output stream...")
            out_stream.stop_stream()
            out_stream.start_stream()
            
    except Exception as e:
        print(ts(), f"Error checking audio streams: {e}")

# =========================
# PLAYBACK - FIXED FOR MUTE MODE
# =========================
stop_playback = threading.Event()
playback_thread = None

def playback_worker():
    print(ts(), "[Playback] started - MUTE mode active")
    last_cleanup = time()
    frames_played = 0
    frames_muted = 0
    frames_silence = 0

    while not stop_playback.is_set():
        # Periodic cleanup of muted indices
        current_time = time()
        if current_time - last_cleanup > MUTED_CLEANUP_INTERVAL:
            cleanup_muted_indices()
            last_cleanup = current_time

        # Get frame from audio buffer
        with audio_lock:
            if len(audio_buffer) > 0:
                try:
                    frame_bytes, idx, ts_cap = audio_buffer.popleft()
                except IndexError:  # Handle race condition
                    frame_bytes, idx, ts_cap = SILENCE_FRAME, None, None
            else:
                frame_bytes, idx, ts_cap = SILENCE_FRAME, None, None

        # Add to delay queue
        delay_queue.append((frame_bytes, idx))

        # FIXED: Play audio after delay, ensuring clean audio passes through
        if len(delay_queue) >= DELAY_FRAMES:
            frame_to_play, frame_idx = delay_queue.popleft()
            
            if frame_idx is not None:
                # Check if this frame should be muted
                with muted_lock:
                    should_mute = frame_idx in muted_indices
                
                if should_mute:
                    frame_to_play = replacement_frame()
                    frames_muted += 1
                    if frames_muted % 10 == 0:  # Reduce spam
                        print(ts(), f"[Mute] Frame {frame_idx} muted (total: {frames_muted})")
                else:
                    frames_played += 1
                    # Occasional debug for clean frames
                    if frames_played % 50 == 0:
                        print(ts(), f"[Play] Playing clean audio (total clean: {frames_played})")
            else:
                # No frame index means it's a silence frame
                frames_silence += 1
                
            try:
                out_stream.write(frame_to_play)
            except IOError as e:
                if e.errno == -32:  # ALSA underrun
                    print(ts(), "[Audio] ALSA underrun, restarting stream")
                    try:
                        out_stream.stop_stream()
                        out_stream.start_stream()
                    except Exception as restart_e:
                        print(ts(), f"[Audio] Failed to restart stream: {restart_e}")
                        break
        else:
            # Queue not full yet, add small delay to prevent busy waiting
            sleep(0.001)
            
    print(ts(), f"[Playback] stopped. Stats: {frames_played} clean, {frames_muted} muted, {frames_silence} silence")

def start_playback():
    global playback_thread
    if playback_thread and playback_thread.is_alive():
        return
    stop_playback.clear()
    playback_thread = threading.Thread(target=playback_worker, daemon=True)
    playback_thread.start()

def stop_playback_thread():
    global playback_thread
    if playback_thread:
        stop_playback.set()
        playback_thread.join(timeout=2)
        playback_thread = None

# =========================
# MODE SWITCHING - FIXED
# =========================
def set_mode(new_mode):
    global current_mode
    
    print(ts(), f"Switching from {current_mode} to {new_mode}")
    
    with mode_lock:
        current_mode = new_mode

    if new_mode == MODE_UNMUTE:
        print(ts(), "Switching to UNMUTE mode...")
        
        # Stop playback thread first
        stop_playback_thread()
        
        # Clear buffers to prevent old data from playing
        with audio_lock:
            audio_buffer.clear()
        delay_queue.clear()
        
        # Clear muted indices
        with muted_lock:
            muted_indices.clear()
            
        # Set LED and announce mode
        led_green()
        print(ts(), "MODE = UNMUTE (direct audio pass-through)")
        print(ts(), "Audio should now play directly without delay")
        
    else:
        print(ts(), "Switching to MUTE mode...")
        
        # Clear any existing state first
        with audio_lock:
            audio_buffer.clear()
        delay_queue.clear()
        
        # Start the playback thread for delayed/filtered audio
        start_playback()
        
        # Set LED and announce mode  
        led_red()
        print(ts(), "MODE = MUTE (selective filtering with 2s delay)")
        print(ts(), "Building delay buffer - audio will start in ~2 seconds")

# =========================
# BUTTON - ENHANCED WITH STREAM CHECKING
# =========================
def button_watcher():
    last = GPIO.input(BUTTON_PIN)
    last_time = 0
    while True:
        try:
            s = GPIO.input(BUTTON_PIN)
            if s != last:
                now = time()
                if now - last_time > 0.2 and s == GPIO.LOW:
                    last_time = now
                    nm = MODE_MUTE if current_mode == MODE_UNMUTE else MODE_UNMUTE
                    
                    # Check audio streams before switching
                    check_audio_streams()
                    
                    # Switch mode
                    set_mode(nm)
                    
                    # Wait a moment then check streams again
                    sleep(0.5)
                    check_audio_streams()
                    
                last = s
        except Exception as e:
            print(ts(), f"[Button] Error: {e}")
        sleep(0.01)

# =========================
# TISANE REPORTING - COMPLETE IMPLEMENTATION
# =========================
def run_tisane_and_report(transcript_text):
    """
    Process transcript through Tisane API for comprehensive child safety monitoring.
    """
    if not os.path.exists(TISANE_SCRIPT):
        print(ts(), f"[Tisane] Script not found: {TISANE_SCRIPT}")
        return

    try:
        print(ts(), f"[Tisane] Processing: '{transcript_text}'")
        proc = subprocess.Popen(["bash",TISANE_SCRIPT, transcript_text],
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True)
        stdout, stderr = proc.communicate(timeout=15)

        if stderr:
            print(ts(), "[Tisane] Error:", stderr)

        if not stdout.strip():
            print(ts(), "[Tisane] No output received")
            return

        try:
            obj = json.loads(stdout)
            
            # Handle error responses
            if "error" in obj:
                print(ts(), f"[Tisane] API Error: {obj['error']}")
                return
                
            # Extract abuse instances from Tisane response
            abuse_instances = obj.get("abuse", [])
            
            if abuse_instances:
                print(ts(), f"[Tisane] Found {len(abuse_instances)} abuse instances")
                
                # Ensure report directory exists
                os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)

                for abuse in abuse_instances:
                    # Extract all Tisane abuse fields
                    abuse_type = abuse.get("type", "unknown")
                    severity = abuse.get("severity", "unknown")
                    text_fragment = abuse.get("text", transcript_text)
                    offset = abuse.get("offset", 0)
                    length = abuse.get("length", 0)
                    sentence_index = abuse.get("sentence_index", 0)
                    tags = abuse.get("tags", [])
                    explanation = abuse.get("explanation", "")
                    
                    # Create timestamp
                    now = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
                    
                    # Build comprehensive log entry
                    log_parts = [
                        f'type "{abuse_type}"',
                        f'severity "{severity}"',
                        f'source "tisane_api"'
                    ]
                    
                    # Add additional details if available
                    if tags:
                        tags_str = ",".join(tags)
                        log_parts.append(f'tags "{tags_str}"')
                    
                    if explanation:
                        # Clean up explanation for logging
                        clean_explanation = explanation.replace('"', "'").replace('\n', ' ')
                        log_parts.append(f'explanation "{clean_explanation}"')
                    
                    # Add position and context info
                    log_parts.append(f'offset {offset}')
                    log_parts.append(f'length {length}')
                    log_parts.append(f'sentence_index {sentence_index}')
                    
                    # Create the full log entry
                    log_entry = f'{now} | {" | ".join(log_parts)} | {text_fragment}'
                    
                    try:
                        with open(REPORT_FILE, "a", encoding='utf-8') as f:
                            f.write(log_entry + '\n')
                            f.flush()
                        print(ts(), f"[Tisane] Logged: {abuse_type} ({severity}) - {text_fragment}")
                        
                        # Additional alert for high-severity issues
                        if severity in ["high", "extreme"]:
                            print(ts(), f"[ALERT] HIGH SEVERITY {abuse_type.upper()}: {text_fragment}")
                            
                    except IOError as e:
                        print(ts(), f"[Tisane] Failed to write report: {e}")
            else:
                print(ts(), f"[Tisane] No abuse detected in: '{transcript_text}'")

        except json.JSONDecodeError as e:
            print(ts(), f"[Tisane] JSON decode error: {e}")
            print(ts(), f"[Tisane] Raw output: {stdout[:200]}...")

    except subprocess.TimeoutExpired:
        proc.kill()
        print(ts(), "[Tisane] Process timeout")
    except Exception as e:
        print(ts(), f"[Tisane] Exception: {e}")

# =========================
# ASSEMBLYAI - FIXED FOR AUDIO FLOW
# =========================
AAI_URL = f"wss://api.assemblyai.com/v2/realtime/ws?sample_rate={RATE}"

async def send_receive():
    global frame_index
    reconnect_delay = 1
    max_reconnect_delay = 30

    # Check if we have a valid API key
    if auth_key == "YOUR_ASSEMBLYAI_API_KEY_HERE":
        print(ts(), "ERROR: Please set your AssemblyAI API key in configure.py")
        print("You can get one from: https://www.assemblyai.com/")
        return

    print(f'{ts()} Connecting to {AAI_URL}')

    try:
        async with websockets.connect(
            AAI_URL,
            extra_headers=(("Authorization", auth_key),),
            ping_interval=5,
            ping_timeout=20,
            close_timeout=10
        ) as ws:
            try:
                await asyncio.wait_for(ws.recv(), timeout=10)
                reconnect_delay = 1  # Reset delay on successful connection
            except asyncio.TimeoutError:
                print(ts(), "[WebSocket] Connection timeout")
                return
            except Exception as e:
                print(ts(), f"[WebSocket] Initial connection error: {e}")
                return

            print(ts(), "WebSocket connected successfully")

            recent_frames = deque(maxlen=int(RATE*2/FRAMES_PER_BUFFER))  # last 2s for muting

            async def sender():
                global frame_index
                unmute_frame_count = 0
                
                try:
                    while True:
                        data = in_stream.read(FRAMES_PER_BUFFER, exception_on_overflow=False)

                        idx = frame_index
                        frame_index += 1

                        # ALWAYS add to buffer in both modes for consistency
                        with audio_lock:
                            audio_buffer.append((data, idx, time()))
                        recent_frames.append(idx)

                        with mode_lock:
                            m = current_mode

                        # In UNMUTE mode, play directly
                        if m == MODE_UNMUTE:
                            try:
                                out_stream.write(data)
                                unmute_frame_count += 1
                                
                                # Debug output every 50 frames (about 5 seconds)
                                if unmute_frame_count % 50 == 0:
                                    print(ts(), f"UNMUTE: Played {unmute_frame_count} frames directly")
                                    
                            except IOError as e:
                                if e.errno == -32:
                                    print(ts(), "UNMUTE: ALSA underrun, restarting output stream")
                                    try:
                                        out_stream.stop_stream()
                                        out_stream.start_stream()
                                        print(ts(), "UNMUTE: Output stream restarted")
                                    except Exception as restart_e:
                                        print(ts(), f"UNMUTE: Failed to restart stream: {restart_e}")
                                else:
                                    print(ts(), f"UNMUTE: Audio output error: {e}")
                        else:
                            # Reset counter when not in unmute mode
                            unmute_frame_count = 0

                        # Send to AssemblyAI regardless of mode
                        try:
                            await ws.send(json.dumps({"audio_data": base64.b64encode(data).decode()}))
                        except websockets.exceptions.ConnectionClosed:
                            print(ts(), "[WebSocket] Connection closed during send")
                            break
                        except Exception as e:
                            print(ts(), f"[WebSocket] Send error: {e}")
                            break

                        await asyncio.sleep(0.005)
                except Exception as e:
                    print(ts(), f"[Sender] Error: {e}")

            async def receiver():
                try:
                    while True:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            print(ts(), "[WebSocket] Receive timeout")
                            break
                        except websockets.exceptions.ConnectionClosed:
                            print(ts(), "[WebSocket] Connection closed during receive")
                            break
                        except Exception as e:
                            print(ts(), f"[WebSocket] Receive error: {e}")
                            break

                        try:
                            obj = json.loads(msg)
                        except json.JSONDecodeError:
                            continue

                        # PartialTranscript for immediate regex muting (only in MUTE mode)
                        if obj.get('message_type') == 'PartialTranscript':
                            text = obj.get('text', '').strip()
                            if text:
                                print(ts(), f"[PartialTranscript]: {text}")
                                
                                with mode_lock:
                                    m = current_mode
                                
                                # Only apply muting in MUTE mode
                                if m == MODE_MUTE and BAD_REGEX and BAD_REGEX.search(text):
                                    mark_frames_for_muting(list(recent_frames))
                                    print(ts(), "[PartialTranscript MUTED]:", text)

                        # FinalTranscript for comprehensive Tisane analysis (both modes)
                        elif obj.get('message_type') == 'FinalTranscript':
                            text = obj.get('text', '').strip()
                            if text:
                                print(ts(), "Transcript (Final):", text)
                                # Run Tisane analysis in background thread
                                threading.Thread(target=run_tisane_and_report,
                                               args=(text,), daemon=True).start()
                except Exception as e:
                    print(ts(), f"[Receiver] Error: {e}")

            await asyncio.gather(sender(), receiver(), return_exceptions=True)

    except Exception as e:
        print(ts(), f"[WebSocket] Connection error: {e}")
        # Exponential backoff for reconnection
        await asyncio.sleep(min(reconnect_delay, max_reconnect_delay))
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

# =========================
# STATUS MONITORING
# =========================
def status_monitor():
    """Monitor system status and provide periodic updates"""
    while True:
        sleep(30)  # Status update every 30 seconds
        
        with audio_lock:
            buffer_size = len(audio_buffer)
        
        with muted_lock:
            muted_count = len(muted_indices)
            
        with mode_lock:
            current_mode_status = current_mode
            
        queue_size = len(delay_queue)
        
        print(ts(), f"[Status] Mode: {current_mode_status}, Buffer: {buffer_size}/{BUFFER_FRAMES}, "
                   f"Queue: {queue_size}/{DELAY_FRAMES}, Muted: {muted_count}")

# =========================
# MAIN - COMPLETE IMPLEMENTATION
# =========================
def main():
    print("=" * 60)
    print("COMPREHENSIVE CHILD SAFETY SPEECH MONITOR v2.1")
    print("=" * 60)
    print(f"Bad words file: {BAD_FILE}")
    print(f"Tisane script: {TISANE_SCRIPT}")
    print(f"Report file: {REPORT_FILE}")
    print("")
    print("Monitoring for all harmful content types:")
    print("- Profanity and vulgar language")
    print("- Personal attacks and cyberbullying")
    print("- Sexual content and advances")
    print("- Violence, threats, and criminal activity")
    print("- Hate speech and bigotry")
    print("- Drug references and adult content")
    print("- Mental health concerns")
    print("- Disturbing and inappropriate content")
    print("")
    print("MODES:")
    print("- UNMUTE: Direct audio pass-through + Tisane logging")
    print("- MUTE: 2s delayed audio with selective filtering + Tisane logging")
    print("")
    print("LED Status:")
    print("- Blue: Starting up")
    print("- Green: UNMUTE (direct audio pass-through)")
    print("- Red: MUTE (selective filtering active)")
    print("")
    print("Press the button to toggle between MUTE/UNMUTE modes")
    print("FIXED: Audio now works properly in both modes")
    print("=" * 60)
    
    set_mode(MODE_UNMUTE)
    threading.Thread(target=button_watcher, daemon=True).start()
    threading.Thread(target=status_monitor, daemon=True).start()

    # Cleanup old muted indices periodically
    def cleanup_timer():
        while True:
            sleep(MUTED_CLEANUP_INTERVAL)
            cleanup_muted_indices()

    threading.Thread(target=cleanup_timer, daemon=True).start()

    try:
        while True:
            try:
                asyncio.run(send_receive())
            except KeyboardInterrupt:
                print(ts(), "Shutting down...")
                break
            except Exception as e:
                print(ts(), "Top-level error:", e)
                sleep(2)  # Brief delay before retry
    finally:
        print(ts(), "Cleaning up...")
        stop_playback_thread()
        led_off()
        try:
            GPIO.cleanup()
            in_stream.close()
            out_stream.close()
            p.terminate()
        except Exception as e:
            print(ts(), f"Cleanup error: {e}")
        
        print("")
        print("=" * 60)
        print("SHUTDOWN COMPLETE")
        print(f"Check report log for detected content: {REPORT_FILE}")
        print("=" * 60)

if __name__ == "__main__":
    main()
