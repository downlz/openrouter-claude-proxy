# openrouter_proxy_server_claude_code_v1.0.py
from flask import Flask, request, Response, jsonify
import requests
import os
import json
import uuid
import time

app = Flask(__name__)

# Get your OpenRouter API key from environment
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "openrouter-key")
if not OPENROUTER_API_KEY:
    raise ValueError("OPENROUTER_API_KEY environment variable is not set. Please add it to your .zshrc or environment.")

# Toggle verbose logging
VERBOSE = os.environ.get("PROXY_VERBOSE", "false").lower() == "true"

# Model mapping function (unchanged, but added caching for minor perf)
_model_cache = {}
def map_model_name(claude_model):
    if claude_model in _model_cache:
        return _model_cache[claude_model]
    
    # Map Claude model names to OpenRouter models
    model_mapping = {
        # Claude expects these default models. Override with you preferred model
        "claude-sonnet-4-5-20250929": "openai/gpt-oss-120b:free",       #
        "claude-haiku-4-5-20251001": "openai/gpt-oss-120b:free",
        # Generic model mappings
        "claude-sonnet": "openai/gpt-oss-120b:free",
        "claude-opus": "openai/gpt-oss-20b:free",
        "claude-haiku": "moonshotai/kimi-k2:free",
        "gpt-oss": "openai/gpt-oss-120b:free",
    }
    
    # If the model is already in OpenRouter format, use it directly
    if "/" in claude_model:
        mapped = claude_model
    # Try to map it directly
    elif claude_model in model_mapping:
        mapped = model_mapping[claude_model]
    # Extract the base model name if it contains version info
    elif "claude-sonnet" in claude_model.lower():
        mapped = model_mapping.get("claude-sonnet", "openai/gpt-oss-120b:free")
    elif "claude-opus" in claude_model.lower():
        mapped = model_mapping.get("claude-opus", "openai/gpt-oss-20b:free")
    elif "claude-haiku" in claude_model.lower():
        mapped = model_mapping.get("claude-haiku", "moonshotai/kimi-k2:free")
    else:
        mapped = "openai/gpt-oss-120b:free"
    
    _model_cache[claude_model] = mapped
    return mapped

def log_verbose(*args, **kwargs):
    if VERBOSE:
        print(*args, **kwargs)

def transform_anthropic_to_openrouter(anthropic_data):
    """Transform Anthropic format to OpenRouter format (optimized, less logging)"""
    log_verbose("Transforming Anthropic to OpenRouter format...")
    
    # Handle different message formats
    messages = anthropic_data.get("messages", [])
    
    # Convert Anthropic message format to OpenAI/OpenRouter format if needed
    openrouter_messages = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            # Handle structured content (Anthropic format)
            content_text = ""
            for content_item in msg["content"]:
                if content_item.get("type") == "text":
                    content_text += content_item.get("text", "")
            
            openrouter_messages.append({
                "role": msg.get("role", "user"),
                "content": content_text
            })
        else:
            # Handle simple string content
            openrouter_messages.append({
                "role": msg.get("role", "user"),
                "content": msg.get("content", "")
            })
    
    original_model = anthropic_data.get("model", "openai/gpt-oss-120b:free")
    mapped_model = map_model_name(original_model)
    
    log_verbose(f"Model mapping: {original_model} -> {mapped_model}")
    
    openrouter_data = {
        "model": mapped_model,
        "messages": openrouter_messages,
        "max_tokens": anthropic_data.get("max_tokens", 1000),
        "temperature": anthropic_data.get("temperature", 0.7),
    }
    
    # Add optional parameters if present
    if "top_p" in anthropic_data:
        openrouter_data["top_p"] = anthropic_data["top_p"]
    if "stream" in anthropic_data:
        openrouter_data["stream"] = anthropic_data["stream"]
    
    return openrouter_data

def transform_openrouter_chunk_to_anthropic(chunk):
    """Transform a single OpenRouter streaming chunk to Anthropic SSE format"""
    # OpenRouter chunk example: {"choices": [{"delta": {"content": "text"}}]}
    if "choices" not in chunk or not chunk["choices"]:
        return None
    
    delta = chunk["choices"][0].get("delta", {})
    content_delta = delta.get("content", "")
    if not content_delta:
        return None
    
    # Anthropic delta format
    return {
        "type": "content_block_delta",
        "index": 0,
        "delta": {
            "type": "text_delta",
            "text": content_delta
        }
    }

def stream_response_generator(openrouter_response, original_model):
    """Generator to stream transformed SSE from OpenRouter to Anthropic format"""
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    
    # Initial event
    yield json.dumps({
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": original_model,
            "content": [{"type": "text", "text": ""}],
            "stop_reason": None,
            "stop_sequence": None
        }
    }) + "\n\n"
    
    # Stream chunks
    for line in openrouter_response.iter_lines():
        if line:
            decoded_line = line.decode('utf-8')
            if decoded_line.startswith('data: '):
                data_str = decoded_line[6:]  # Remove 'data: '
                if data_str == '[DONE]':
                    # End event
                    yield json.dumps({
                        "type": "message_stop",
                        "message": {
                            "id": message_id,
                            "stop_reason": "end_turn",
                            "stop_sequence": None
                        }
                    }) + "\n\n"
                    break
                
                try:
                    chunk = json.loads(data_str)
                    anthropic_chunk = transform_openrouter_chunk_to_anthropic(chunk)
                    if anthropic_chunk:
                        yield json.dumps(anthropic_chunk) + "\n\n"
                except json.JSONDecodeError:
                    # Ignore non-JSON lines like comments (: OPENROUTER PROCESSING)
                    continue
    
    # Final done
    yield "data: [DONE]\n\n"

# Handle both /v1/messages and /anthropic/v1/messages
@app.route('/v1/messages', methods=['POST'])
@app.route('/anthropic/v1/messages', methods=['POST'])
def proxy_to_openrouter():
    try:
        # Get the request from Claude Code
        data = request.json
        
        # Conditional verbose logging to reduce overhead
        if VERBOSE:
            print("=" * 50)
            print("INCOMING REQUEST FROM CLAUDE CODE:")
            print(f"Path: {request.path}")
            print(f"Method: {request.method}")
            print(f"Headers: {dict(request.headers)}")
            print(f"Body: {json.dumps(data, indent=2)}")
            print("=" * 50)
        
        # Validate the request data (quick checks)
        if not data or "messages" not in data:
            return jsonify({
                "error": {"type": "invalid_request_error", "message": "Missing required field: messages"}
            }), 400
        
        # Transform the request format
        openrouter_data = transform_anthropic_to_openrouter(data)
        
        if VERBOSE:
            print("TRANSFORMED DATA FOR OPENROUTER:")
            print(json.dumps(openrouter_data, indent=2))
            print("=" * 50)
        
        # Make the request to OpenRouter
        headers = {
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://your-site.com",
            "X-Title": "Claude Code Proxy"
        }
        
        log_verbose("SENDING REQUEST TO OPENROUTER...")
        
        is_streaming = openrouter_data.get("stream", False)
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers=headers,
            json=openrouter_data,
            timeout=30,
            stream=is_streaming  # Enable streaming if requested
        )
        
        log_verbose(f"OPENROUTER RESPONSE STATUS: {response.status_code}")
        
        if response.status_code != 200:
            log_verbose(f"ERROR FROM OPENROUTER: {response.status_code}")
            log_verbose(f"ERROR RESPONSE: {response.text}")
            
            try:
                error_data = response.json()
                error_message = error_data.get("error", {}).get("message", "Unknown error")
                error_type = error_data.get("error", {}).get("type", "api_error")
            except:
                error_message = response.text
                error_type = "api_error"
            
            return jsonify({
                "error": {"type": error_type, "message": f"OpenRouter API returned status {response.status_code}: {error_message}"}
            }), response.status_code
        
        original_model = data.get("model", "claude-3-sonnet-20240229")
        
        if is_streaming:
            # Streaming response: Use generator for SSE
            def generate_stream():
                try:
                    for event in stream_response_generator(response, original_model):
                        yield event
                except Exception as e:
                    log_verbose(f"STREAM ERROR: {str(e)}")
                    yield json.dumps({"type": "error", "error": {"type": "internal_error", "message": str(e)}}) + "\n\n"
                finally:
                    response.close()
            
            return Response(
                generate_stream(),
                mimetype='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'Connection': 'keep-alive',
                    'X-Accel-Buffering': 'no'  # Disable buffering in proxies like nginx
                }
            )
        else:
            # Non-streaming: Parse full response (optimized from original)
            try:
                openrouter_response = response.json()
            except json.JSONDecodeError as e:
                log_verbose(f"ERROR: Failed to parse OpenRouter response as JSON: {e}")
                log_verbose(f"RAW RESPONSE: {response.text}")
                return jsonify({
                    "error": {"type": "api_error", "message": "Invalid JSON response from OpenRouter"}
                }), 500
            
            log_verbose("OPENROUTER RESPONSE:")
            log_verbose(json.dumps(openrouter_response, indent=2))
            log_verbose("=" * 50)
            
            # Check for errors (quick)
            if "error" in openrouter_response:
                log_verbose(f"ERROR IN OPENROUTER RESPONSE: {openrouter_response['error']}")
                return jsonify({
                    "error": {"type": "api_error", "message": f"OpenRouter API error: {openrouter_response['error']}"}
                }), 400
            
            if "choices" not in openrouter_response or not openrouter_response["choices"]:
                return jsonify({
                    "error": {"type": "api_error", "message": "Unexpected response format from OpenRouter - missing 'choices' field"}
                }), 500
            
            message = openrouter_response["choices"][0]["message"]
            content = message.get("content", "") or message.get("reasoning", "")
            
            if not content:
                return jsonify({
                    "error": {"type": "api_error", "message": "Unexpected message format from OpenRouter - missing content"}
                }), 500
            
            # Determine stop reason
            finish_reason = openrouter_response["choices"][0].get("finish_reason", "stop")
            stop_reason = "max_tokens" if finish_reason == "length" else "end_turn"
            
            # Build response (unchanged)
            message_id = f"msg_{uuid.uuid4().hex[:24]}"
            anthropic_response = {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": content}],
                "model": original_model,
                "stop_reason": stop_reason,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": openrouter_response.get("usage", {}).get("prompt_tokens", 0),
                    "output_tokens": openrouter_response.get("usage", {}).get("completion_tokens", 0)
                }
            }
            
            log_verbose("FINAL ANTHROPIC-FORMAT RESPONSE:")
            log_verbose(json.dumps(anthropic_response, indent=2))
            log_verbose("=" * 50)
            
            return jsonify(anthropic_response)
    
    except Exception as e:
        log_verbose(f"UNEXPECTED ERROR: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": {"type": "internal_error", "message": f"Internal server error: {str(e)}"}
        }), 500

# Health check (unchanged)
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "claude-code-proxy"})

if __name__ == '__main__':
    print("Starting Claude Code Proxy Server (v1.0 - Streaming Enabled)...")
    print(f"OpenRouter API Key: {'SET' if OPENROUTER_API_KEY else 'NOT SET'}")
    print(f"Verbose Logging: {VERBOSE}")
    print("Default model: openai/gpt-oss-120b:free")
    print("Set PROXY_VERBOSE=true for detailed logs.")
    app.run(port=8000, debug=VERBOSE)  # Debug only if verbose