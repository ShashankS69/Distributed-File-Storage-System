from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit
import os
import threading
from queue import Queue
import time
import json
import datetime
from collections import defaultdict

app = Flask(__name__, static_folder='static')
socketio = SocketIO(app, cors_allowed_origins="*")

# Directories for storage
ORIGINAL_DIR = 'storage/original'
REPLICA_DIR = 'storage/replica'
os.makedirs(ORIGINAL_DIR, exist_ok=True)
os.makedirs(REPLICA_DIR, exist_ok=True)

# Queue for handling uploads
upload_queue = Queue()
lock = threading.Lock()

# Ricart-Agrawala algorithm implementation
clients = {}  # Dictionary to track active clients
request_queue = []  # Queue for requests
resource_locked = False
current_holder = None
logical_clock = 0
system_logs = []


def log_event(message):
    """Add event to system logs and emit to clients"""
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    log_entry = {"timestamp": timestamp, "message": message}
    system_logs.append(log_entry)
    if len(system_logs) > 100:
        system_logs.pop(0)  # Keep only the last 100 logs
    socketio.emit('log_event', log_entry)

# Client upload handler


def process_uploads():
    global resource_locked, current_holder
    while True:
        client_id, file_content, filename = upload_queue.get()
        try:
            if file_content and filename:
                # Update RA state
                resource_locked = True
                current_holder = client_id
                log_event(
                    f"Ricart-Agrawala: Granting access to client {client_id}")
                socketio.emit('queue_update', {
                              'message': f"Client {client_id} granted access to upload {filename}"})

                try:
                    # Process the file
                    file_path = os.path.join(ORIGINAL_DIR, filename)
                    with open(file_path, 'wb') as f_dest:
                        f_dest.write(file_content)

                    log_event(f"File saved to original volume: {filename}")

                    # Ensure replica directory exists
                    os.makedirs(REPLICA_DIR, exist_ok=True)

                    # Replicate file
                    replica_path = os.path.join(REPLICA_DIR, filename)
                    try:
                        # Use the saved content to write to replica
                        with open(replica_path, 'wb') as f_dest:
                            f_dest.write(file_content)

                        # Verify replication was successful
                        if os.path.exists(replica_path) and os.path.getsize(replica_path) == os.path.getsize(file_path):
                            log_event(
                                f"File successfully replicated: {filename}")
                        else:
                            log_event(
                                f"Replication verification failed for {filename}")
                    except Exception as replica_error:
                        log_event(
                            f"Error during replication of {filename}: {str(replica_error)}")
                        # Try one more time with a different method
                        try:
                            import shutil
                            shutil.copyfile(file_path, replica_path)
                            log_event(
                                f"File replicated using fallback method: {filename}")
                        except Exception as fallback_error:
                            log_event(
                                f"Fallback replication also failed: {str(fallback_error)}")

                    # Notify clients
                    socketio.emit('file_uploaded', {
                                  'client_id': client_id, 'filename': filename})
                    log_event(
                        f"File {filename} uploaded successfully by client {client_id}")
                except Exception as e:
                    log_event(
                        f"Error processing file from client {client_id}: {str(e)}")
                finally:
                    # Always release the resource, even if an error occurred
                    resource_locked = False
                    current_holder = None
                    log_event(
                        f"Ricart-Agrawala: Client {client_id} released resource")

                # Process queued requests
                with lock:
                    if request_queue:
                        # Check the format of the queued item
                        next_item = request_queue.pop(0)

                        if isinstance(next_item, tuple) and len(next_item) == 3:
                            # New format: (client_id, file_content, filename)
                            next_client, next_content, next_filename = next_item
                            log_event(
                                f"Ricart-Agrawala: Processing next queued request from client {next_client}")
                            socketio.emit('queue_update', {
                                          'message': f"Processing queued request from client {next_client}"})
                            upload_queue.put(
                                (next_client, next_content, next_filename))
                        else:
                            # Old format or delete request: just client_id
                            next_client = next_item
                            log_event(
                                f"Ricart-Agrawala: Next request from client {next_client} will be processed")
                            socketio.emit('process_next_request', {
                                          'client_id': next_client})
        except Exception as e:
            log_event(f"Critical error in upload processing: {str(e)}")
            # Make sure resource is released in case of critical error
            resource_locked = False
            current_holder = None
        finally:
            upload_queue.task_done()


# Start the upload processing thread
threading.Thread(target=process_uploads, daemon=True).start()


@app.route('/')
def home():
    return send_from_directory('templates', 'index.html')


@app.route('/upload', methods=['POST'])
def upload_file():
    client_id = request.form.get('client_id')
    file = request.files.get('file')

    if not client_id:
        return jsonify({'error': 'Client ID is required'}), 400

    if not file:
        return jsonify({'error': 'No file provided'}), 400

    # Save the file content and filename immediately
    # to prevent "I/O operation on closed file" error
    try:
        file_content = file.read()
        filename = file.filename
        file_size = len(file_content)

        if file_size == 0:
            return jsonify({'error': 'Empty file provided'}), 400

        log_event(
            f"File received from client {client_id}: {filename}, size: {file_size} bytes")
    except Exception as e:
        log_event(f"Error reading file from client {client_id}: {str(e)}")
        return jsonify({'error': f'Error processing file: {str(e)}'}), 500

    # Implement Ricart-Agrawala request
    global logical_clock
    logical_clock += 1
    request_timestamp = logical_clock

    # If resource is locked, add to queue
    if resource_locked:
        with lock:
            # Store tuple of client_id, file_content, and filename
            request_queue.append((client_id, file_content, filename))
        log_event(
            f"Ricart-Agrawala: Client {client_id} request added to queue (timestamp: {request_timestamp})")
        socketio.emit('queue_update', {
                      'message': f"Client {client_id} request for {filename} queued"})
        return jsonify({'message': f'Resource busy. File {filename} added to queue'}), 200
    else:
        # Process immediately if resource is available
        with lock:
            # Pass file_content and filename instead of the file object
            upload_queue.put((client_id, file_content, filename))
        log_event(
            f"Ricart-Agrawala: Client {client_id} request being processed immediately (timestamp: {request_timestamp})")
        return jsonify({'message': f'File {filename} added to processing queue'}), 200


@app.route('/files', methods=['GET'])
def list_files():
    files = os.listdir(ORIGINAL_DIR)
    return jsonify({'files': files})


@app.route('/volume-files/<volume>', methods=['GET'])
def get_volume_files(volume):
    """Get detailed file information for a specific volume"""
    if volume == 'original':
        dir_path = ORIGINAL_DIR
    elif volume == 'replica':
        dir_path = REPLICA_DIR
    else:
        return jsonify({'error': 'Invalid volume specified'}), 400

    files = []
    for filename in os.listdir(dir_path):
        file_path = os.path.join(dir_path, filename)
        if os.path.isfile(file_path):
            file_info = {
                'name': filename,
                'size': os.path.getsize(file_path),
                'created': os.path.getctime(file_path),
                'modified': os.path.getmtime(file_path)
            }
            files.append(file_info)

    return jsonify({'files': files})


@app.route('/delete/<filename>', methods=['DELETE'])
def delete_file(filename):
    client_id = 'Unknown'
    if request.is_json:
        client_id = request.json.get('client_id', 'Unknown')

    # Implement Ricart-Agrawala for delete operation
    global logical_clock, resource_locked
    logical_clock += 1
    request_timestamp = logical_clock

    # If resource is locked, add to queue
    if resource_locked:
        with lock:
            request_queue.append(client_id)
        log_event(
            f"Ricart-Agrawala: Client {client_id} delete request added to queue (timestamp: {request_timestamp})")
        return jsonify({'message': f'Resource busy. Delete operation for {filename} queued'}), 200

    # Process delete operation
    original_path = os.path.join(ORIGINAL_DIR, filename)
    replica_path = os.path.join(REPLICA_DIR, filename)

    # Lock resource
    resource_locked = True
    current_holder = client_id
    log_event(
        f"Ricart-Agrawala: Client {client_id} granted access to delete {filename}")

    try:
        # Delete files from both volumes
        deleted_original = False
        deleted_replica = False

        # Delete from original volume
        if os.path.exists(original_path):
            try:
                os.remove(original_path)
                deleted_original = True
                log_event(f"File deleted from original volume: {filename}")
            except Exception as e:
                log_event(f"Error deleting from original volume: {str(e)}")

        # Delete from replica volume
        if os.path.exists(replica_path):
            try:
                os.remove(replica_path)
                deleted_replica = True
                log_event(f"File deleted from replica volume: {filename}")
            except Exception as e:
                log_event(f"Error deleting from replica volume: {str(e)}")

        # Check if deletion was successful
        if deleted_original and deleted_replica:
            message = f"File {filename} successfully deleted from both volumes"
        elif deleted_original:
            message = f"File {filename} deleted from original volume only"
        elif deleted_replica:
            message = f"File {filename} deleted from replica volume only"
        else:
            message = f"Failed to delete {filename} from either volume"

        log_event(message)

        # Notify clients
        socketio.emit('file_deleted', {
                      'filename': filename, 'client_id': client_id})

    except Exception as e:
        log_event(f"Critical error during file deletion: {str(e)}")
    finally:
        # Release resource
        resource_locked = False
        current_holder = None
        log_event(
            f"Ricart-Agrawala: Client {client_id} released resource after delete operation")

    # Check if there are pending requests
    with lock:
        if request_queue:
            next_client = request_queue.pop(0)
            log_event(
                f"Ricart-Agrawala: Next request from client {next_client} will be processed")
            socketio.emit('process_next_request', {'client_id': next_client})

    return jsonify({'message': f'File {filename} deletion process completed'}), 200


@app.route('/register-client', methods=['POST'])
def register_client():
    """Register a client in the system"""
    if not request.is_json:
        return jsonify({'error': 'Request must be JSON'}), 400

    client_id = request.json.get('client_id')
    if not client_id:
        return jsonify({'error': 'Client ID is required'}), 400

    # Add client to tracking dictionary
    clients[client_id] = {
        'last_active': time.time(),
        'socket_id': request.sid if hasattr(request, 'sid') else None
    }

    log_event(f"Client registered: {client_id}")
    socketio.emit('client_registered', {'client_id': client_id})

    return jsonify({'message': f'Client {client_id} registered successfully'}), 200


@app.route('/system-logs', methods=['GET'])
def get_system_logs():
    """Return the system logs"""
    return jsonify({'logs': system_logs})


@app.route('/system-status', methods=['GET'])
def get_system_status():
    """Get overall system status including file information, active clients, and queue details"""
    global resource_locked, current_holder

    # Count files and calculate storage statistics
    original_size = 0
    original_files = []
    if os.path.exists(ORIGINAL_DIR):
        original_files = [f for f in os.listdir(
            ORIGINAL_DIR) if os.path.isfile(os.path.join(ORIGINAL_DIR, f))]
        for filename in original_files:
            file_path = os.path.join(ORIGINAL_DIR, filename)
            if os.path.exists(file_path):
                original_size += os.path.getsize(file_path)

    replica_size = 0
    replica_files = []
    if os.path.exists(REPLICA_DIR):
        replica_files = [f for f in os.listdir(
            REPLICA_DIR) if os.path.isfile(os.path.join(REPLICA_DIR, f))]
        for filename in replica_files:
            file_path = os.path.join(REPLICA_DIR, filename)
            if os.path.exists(file_path):
                replica_size += os.path.getsize(file_path)

    # Get activity data from system logs
    upload_count = len(
        [log for log in system_logs if "uploaded successfully" in log["message"]])
    delete_count = len(
        [log for log in system_logs if "deleted from" in log["message"]])

    # Process the queue data for visualization
    queue_data = []
    for item in request_queue:
        if isinstance(item, tuple) and len(item) >= 3:
            # Format for upload requests: (client_id, file_content, filename)
            queue_data.append({
                "clientId": item[0],
                "operation": f"upload {item[2]}",
                "type": "upload"
            })
        else:
            # Format for delete requests or old format: client_id
            queue_data.append({
                "clientId": item if isinstance(item, str) else "Unknown",
                "operation": "operation",
                "type": "other"
            })

    # Compile the complete status
    status = {
        "totalFiles": len(original_files),
        "storageUsed": original_size + replica_size,
        "activeClients": len(clients),
        "queueLength": len(request_queue),
        "ricart": {
            "locked": resource_locked,
            "currentHolder": current_holder
        },
        "queue": queue_data,
        "activityData": {
            "uploads": upload_count,
            "deletions": delete_count,
            "queuedRequests": len(request_queue)
        },
        "volumeData": {
            "original": original_size,
            "replica": replica_size
        }
    }

    return jsonify(status)


@socketio.on('connect')
def handle_connect():
    print(f"Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    # Find and remove the disconnected client
    for client_id, data in list(clients.items()):
        if data.get('socket_id') == request.sid:
            clients.pop(client_id, None)
            log_event(f"Client disconnected: {client_id}")
            socketio.emit('client_disconnected', {'client_id': client_id})
            break


if __name__ == '__main__':
    socketio.run(app, debug=True, port=6920)
