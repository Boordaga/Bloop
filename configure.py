#!/usr/bin/env python3
"""
Configuration file for the speech monitoring system
Now fetches AssemblyAI and Tisane API keys securely from your VPS via WireGuard.
"""

import requests

# -------------------------------
# VPS Key Server Configuration
# -------------------------------
# Change this to your VPS WireGuard IP if different
VPS_IP = "10.8.0.1"
VPS_PORT = 5050
KEYS_URL = f"http://{VPS_IP}:{VPS_PORT}/get-keys"

def fetch_keys():
    """
    Fetch AssemblyAI and Tisane API keys from VPS.
    Returns (assemblyai_key, tisane_key) or (None, None) if failed.
    """
    try:
        print("[INFO] Fetching API keys securely from VPS...")
        response = requests.get(KEYS_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        return data.get("assemblyai"), data.get("tisane")
    except Exception as e:
        print(f"[ERROR] Could not fetch keys from {KEYS_URL}: {e}")
        return None, None

# -------------------------------
# API Keys (Secure Fetch)
# -------------------------------
auth_key, tisane_api_key = fetch_keys()

if not auth_key or not tisane_api_key:
    print("[WARNING] API keys not loaded! Speech monitoring may fail.")
else:
    print("[OK] API keys loaded successfully.")

# -------------------------------
# File Paths
# -------------------------------
BAD_WORDS_FILE = "/home/pi/sr/bad.txt"
TISANE_SCRIPT = "/home/pi/sr/tesani_modified.sh"  # Your shell script location
REPORT_FILE = "/home/pi/sr/report.log"
DEBUG_LOG_FILE = "/home/pi/sr/tisane_debug.log"

# -------------------------------
# Audio Settings
# -------------------------------
SAMPLE_RATE = 8000
CHANNELS = 1
FRAMES_PER_BUFFER = 800

# -------------------------------
# GPIO Pins
# -------------------------------
RED_LED_PIN = 12
GREEN_LED_PIN = 19
BLUE_LED_PIN = 13
BUTTON_PIN = 11

# -------------------------------
# Filtering Settings
# -------------------------------
BUFFER_SECONDS = 4.0
DELAY_SECONDS = 2.0
MUTED_CLEANUP_INTERVAL = 10.0

# -------------------------------
# Beep Settings
# -------------------------------
BEEP_FREQUENCY_HZ = 1000
BEEP_VOLUME = 0.3
REPLACE_MODE = "beep"  # "beep" or "silence"

