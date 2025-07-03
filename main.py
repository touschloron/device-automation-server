from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Optional, List
import uuid
import json
from datetime import datetime
import jwt
import base64
import io
import platform
import subprocess
import tempfile
import os

# Screenshot imports - only import if DISPLAY is available
import os
PYAUTOGUI_AVAILABLE = False
PIL_AVAILABLE = False

# Only try to import GUI libraries if we have a display
if os.environ.get('DISPLAY') or os.name == 'nt':  # Windows or Linux with display
    try:
        import pyautogui
        PYAUTOGUI_AVAILABLE = True
    except (ImportError, Exception):
        PYAUTOGUI_AVAILABLE = False

    try:
        from PIL import Image, ImageGrab
        PIL_AVAILABLE = True
    except (ImportError, Exception):
        PIL_AVAILABLE = False

# Simple models
class DeviceRegistration(BaseModel):
    device_name: str
    device_type: str
    os_info: Optional[str] = None
    capabilities: Optional[List[str]] = []

class AutomationCommand(BaseModel):
    command_id: Optional[str] = None
    action: str
    target: Optional[dict] = None
    parameters: Optional[dict] = None

class ScreenshotResponse(BaseModel):
    screenshot: str  # base64 encoded
    timestamp: str
    resolution: dict
    success: bool

# Initialize FastAPI
app = FastAPI()

# Add CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple storage
connected_devices: Dict[str, WebSocket] = {}
device_registry: Dict[str, dict] = {}
SECRET_KEY = "your-secret-key-123"

def create_token(device_id: str) -> str:
    """Create a simple JWT token"""
    payload = {
        'device_id': device_id,
        'type': 'device'
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')

def verify_token(token: str) -> dict:
    """Verify JWT token"""
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
    except:
        raise ValueError("Invalid token")

# ========== SCREENSHOT FUNCTIONS ==========

def capture_screenshot_pyautogui():
    """Capture screenshot using pyautogui"""
    if not PYAUTOGUI_AVAILABLE:
        raise ImportError("pyautogui not available")
    
    screenshot = pyautogui.screenshot()
    buffer = io.BytesIO()
    screenshot.save(buffer, format='PNG')
    buffer.seek(0)
    
    return {
        'image_data': buffer.getvalue(),
        'resolution': {'width': screenshot.width, 'height': screenshot.height}
    }

def capture_screenshot_pil():
    """Capture screenshot using PIL ImageGrab"""
    if not PIL_AVAILABLE:
        raise ImportError("PIL not available")
    
    screenshot = ImageGrab.grab()
    buffer = io.BytesIO()
    screenshot.save(buffer, format='PNG')
    buffer.seek(0)
    
    return {
        'image_data': buffer.getvalue(),
        'resolution': {'width': screenshot.width, 'height': screenshot.height}
    }

def capture_screenshot():
    """Main screenshot function"""
    os_name = platform.system().lower()
    
    # Try methods based on OS
    if os_name == "darwin":  # macOS
        methods = [capture_screenshot_pyautogui, capture_screenshot_pil]
    elif os_name == "windows":
        methods = [capture_screenshot_pyautogui, capture_screenshot_pil]
    else:  # Linux and others
        methods = [capture_screenshot_pyautogui, capture_screenshot_pil]
    
    last_error = None
    for method in methods:
        try:
            return method()
        except Exception as e:
            last_error = e
            continue
    
    raise RuntimeError(f"All screenshot methods failed. Last error: {last_error}")

@app.get("/")
async def root():
    return {"message": "Device Automation Server is running"}

@app.get("/api/health")
async def health():
    return {
        "status": "healthy",
        "devices_connected": len(connected_devices),
        "devices_registered": len(device_registry),
        "timestamp": datetime.now().isoformat()
    }

@app.post("/api/devices/register")
async def register_device(device: DeviceRegistration):
    """Register a new device"""
    device_id = str(uuid.uuid4())
    
    # Store device
    device_registry[device_id] = {
        "device_id": device_id,
        "name": device.device_name,
        "type": device.device_type,
        "os_info": device.os_info,
        "capabilities": device.capabilities,
        "status": "registered",
        "registered_at": datetime.now().isoformat()
    }
    
    # Create token
    token = create_token(device_id)
    
    print(f"✅ Device registered: {device.device_name} (ID: {device_id})")
    
    return {
        "device_id": device_id,
        "token": token,
        "status": "registered"
    }

@app.post("/api/devices/screenshot")
async def take_screenshot():
    """Capture and return screenshot as base64"""
    # For Railway deployment, we can't take screenshots directly
    # This endpoint exists for API compatibility
    raise HTTPException(
        status_code=501, 
        detail="Screenshot capability not available on headless server. Use connected device client instead."
    )

@app.get("/api/devices")
async def list_devices():
    """List all devices"""
    return {
        "devices": list(device_registry.values()),
        "total": len(device_registry)
    }

@app.post("/api/devices/{device_id}/execute")
async def execute_command(device_id: str, command: AutomationCommand):
    """Send command to device"""
    
    if device_id not in device_registry:
        return {"error": "Device not found"}, 404
    
    if device_id not in connected_devices:
        return {"error": "Device not connected"}, 503
    
    # Set command ID if not provided
    if not command.command_id:
        command.command_id = str(uuid.uuid4())
    
    # Send to device
    try:
        websocket = connected_devices[device_id]
        command_data = {
            "command_id": command.command_id,
            "action": command.action,
            "target": command.target or {},
            "parameters": command.parameters or {},
            "timestamp": datetime.now().isoformat()
        }
        
        await websocket.send_text(json.dumps(command_data))
        
        print(f"📤 Sent command {command.action} to device {device_id}")
        
        return {
            "command_id": command.command_id,
            "status": "sent",
            "device_id": device_id
        }
        
    except Exception as e:
        print(f"❌ Error sending command: {e}")
        return {"error": str(e)}, 500

@app.websocket("/ws/device/{device_id}")
async def device_websocket(websocket: WebSocket, device_id: str):
    """WebSocket for device connection"""
    await websocket.accept()
    
    try:
        # Get auth message
        auth_msg = await websocket.receive_text()
        auth_data = json.loads(auth_msg)
        
        # Simple auth check
        token = auth_data.get('token', '')
        try:
            payload = verify_token(token)
            if payload.get('device_id') != device_id:
                await websocket.close(code=1008, reason="Invalid token")
                return
        except:
            await websocket.close(code=1008, reason="Auth failed")
            return
        
        # Register connection
        connected_devices[device_id] = websocket
        if device_id in device_registry:
            device_registry[device_id]["status"] = "connected"
        
        print(f"🔗 Device {device_id} connected via WebSocket")
        
        # Listen for responses
        try:
            while True:
                message = await websocket.receive_text()
                response = json.loads(message)
                print(f"📨 Response from {device_id}: {response.get('status', 'unknown')}")
                
        except WebSocketDisconnect:
            print(f"🔌 Device {device_id} disconnected")
        
    except Exception as e:
        print(f"❌ WebSocket error: {e}")
    
    finally:
        # Cleanup
        connected_devices.pop(device_id, None)
        if device_id in device_registry:
            device_registry[device_id]["status"] = "disconnected"

if __name__ == "__main__":
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
