from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Optional, List
import asyncio
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

class ScreenshotRequest(BaseModel):
    device_id: str
    description: Optional[str] = None

class ScreenshotTaskResponse(BaseModel):
    request_id: str
    status: str  # "pending", "completed", "failed"
    screenshot: Optional[str] = None
    timestamp: Optional[str] = None
    resolution: Optional[dict] = None
    error: Optional[str] = None

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
screenshot_tasks: Dict[str, dict] = {}  # request_id -> task data

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

@app.post("/api/devices/screenshot/request")
async def request_screenshot(request: ScreenshotRequest):
    """Start a screenshot task and return request_id"""
    import uuid
    
    # Check if device exists and is connected
    if request.device_id not in device_registry:
        raise HTTPException(status_code=404, detail="Device not found")
    
    if request.device_id not in connected_devices:
        raise HTTPException(status_code=503, detail="Device not connected")
    
    # Create unique request ID
    request_id = str(uuid.uuid4())
    
    # Store the task
    screenshot_tasks[request_id] = {
        "status": "pending",
        "device_id": request.device_id,
        "description": request.description,
        "created_at": datetime.now().isoformat(),
        "screenshot": None,
        "error": None
    }
    
    # Send command to device
    try:
        websocket = connected_devices[request.device_id]
        command_data = {
            "command_id": request_id,
            "action": "screenshot",
            "target": {},
            "parameters": {"description": request.description or ""},
            "timestamp": datetime.now().isoformat()
        }
        
        await websocket.send_text(json.dumps(command_data))
        print(f"📸 Screenshot request {request_id} sent to device {request.device_id}")
        
        return {"request_id": request_id, "status": "pending"}
        
    except Exception as e:
        # Mark task as failed
        screenshot_tasks[request_id]["status"] = "failed"
        screenshot_tasks[request_id]["error"] = str(e)
        print(f"❌ Failed to send screenshot request: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to send command: {str(e)}")

@app.get("/api/devices/screenshot/status/{request_id}")
async def get_screenshot_status(request_id: str):
    """Poll for screenshot task status and result"""
    if request_id not in screenshot_tasks:
        raise HTTPException(status_code=404, detail="Screenshot request not found")
    
    task = screenshot_tasks[request_id]
    
    response = ScreenshotTaskResponse(
        request_id=request_id,
        status=task["status"],
        screenshot=task.get("screenshot"),
        timestamp=task.get("completed_at"),
        resolution=task.get("resolution"),
        error=task.get("error")
    )
    
    # Clean up completed tasks after 5 minutes
    if task["status"] in ["completed", "failed"]:
        created_time = datetime.fromisoformat(task["created_at"])
        if (datetime.now() - created_time).total_seconds() > 300:  # 5 minutes
            del screenshot_tasks[request_id]
    
    return response

@app.post("/api/devices/screenshot")
async def take_screenshot():
    """Legacy endpoint - now returns error directing to new endpoints"""
    raise HTTPException(
        status_code=501, 
        detail="This endpoint is deprecated. Use POST /api/devices/screenshot/request and GET /api/devices/screenshot/status/{request_id} instead."
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
             response_data = json.loads(message)
             print(f"📨 Response from {device_id}: {response_data.get('status', 'unknown')}")
        
        # Handle screenshot responses
            command_id = response_data.get("command_id")
                 if command_id and command_id in screenshot_tasks:
                 task = screenshot_tasks[command_id]
            
                     if response_data.get("status") == "success":
                         result = response_data.get("result", {})
                         task["status"] = "completed"
                         task["screenshot"] = result.get("screenshot")
                         task["resolution"] = result.get("resolution")
                         task["completed_at"] = datetime.now().isoformat()
                         print(f"✅ Screenshot {command_id} completed")
                    else:
                        task["status"] = "failed" 
                        task["error"] = response_data.get("error_message", "Unknown error")
                        print(f"❌ Screenshot {command_id} failed")
        
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
