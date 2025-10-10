import os
import re
import csv
from io import StringIO
from flask import Flask, render_template, request, jsonify, send_from_directory, send_file
from flask_cors import CORS
from pydub import AudioSegment
import tempfile

# Built with GitHub Actions
app = Flask(__name__)
CORS(app)

# Global variables for playlist management
current_directory = None
current_playlist = [] # Stores full paths on server
audio_file_map = {}  # Maps filename to full path for nested directories
SUPPORTED_AUDIO_EXTENSIONS = ('.mp3', '.wav', '.ogg')

# Global variable to store parsed log data
parsed_transcription_data = {}

# Global variables for CSV error labeling
csv_error_data = []
csv_file_loaded = False

def time_to_seconds(time_str):
    """Convert HH:MM:SS format to seconds"""
    try:
        if ':' in time_str:
            parts = time_str.split(':')
            if len(parts) == 3:
                hours, minutes, seconds = map(float, parts)
                return hours * 3600 + minutes * 60 + seconds
            elif len(parts) == 2:
                minutes, seconds = map(float, parts)
                return minutes * 60 + seconds
        return float(time_str)  # Try direct conversion as fallback
    except (ValueError, AttributeError):
        return 0.0

def parse_log_content(log_content):
    """
    Parses the log content to extract error segments for each audio file.
    Only supports the new CSV/TSV format as in sample_log.txt.
    Returns a dict mapping audio file basenames to a list of error segments.
    """
    data = {}
    # Auto-detect delimiter: use tab if more tabs than commas, else comma
    tab_count = log_content.count('\t')
    comma_count = log_content.count(',')
    delimiter = '\t' if tab_count > comma_count else ','
    reader = csv.reader(StringIO(log_content), delimiter=delimiter)
    header = next(reader, None)
    # Find relevant column indices
    def col(name):
        try:
            return header.index(name)
        except ValueError:
            return None
    col_audio = col('transcriptFile')
    col_long_start = col('longFormStart')
    col_long_end = col('longFormEnd')
    col_long_error = col('longFormError')
    col_short_error = col('shortFormError')
    col_short_start = col('shortFormStart')
    col_short_end = col('shortFormEnd')
    for row in reader:
        if not row or len(row) <= max(filter(None, [col_audio, col_long_start, col_long_end, col_long_error, col_short_error, col_short_start, col_short_end])):
            continue
        audio_path = row[col_audio]
        filename = os.path.basename(audio_path)
        try:
            long_start = float(row[col_long_start]) if col_long_start is not None else None
            long_end = float(row[col_long_end]) if col_long_end is not None else None
            short_start = float(row[col_short_start]) if col_short_start is not None and row[col_short_start] else None
            short_end = float(row[col_short_end]) if col_short_end is not None and row[col_short_end] else None
        except (ValueError, IndexError):
            long_start = long_end = short_start = short_end = None
        segment = {
            'longFormStart': long_start,
            'longFormEnd': long_end,
            'longFormError': row[col_long_error] if col_long_error is not None else '',
            'shortFormError': row[col_short_error] if col_short_error is not None else '',
            'shortFormStart': short_start,
            'shortFormEnd': short_end
        }
        if filename not in data:
            data[filename] = []
        data[filename].append(segment)
    return data

@app.route('/audio_files/<path:filename>')
def serve_audio_file(filename):
    """
    Serves audio files directly to the client's web browser.
    Now supports nested directories using the audio_file_map.
    """
    # First check if we have a mapping for this filename (from nested directories)
    if filename in audio_file_map:
        file_path = audio_file_map[filename]
        directory = os.path.dirname(file_path)
        file_name = os.path.basename(file_path)
        return send_from_directory(directory, file_name)
    
    # Fallback to old behavior for flat directory structure
    elif current_directory and os.path.exists(os.path.join(current_directory, filename)):
        return send_from_directory(current_directory, filename)
    else:
        return "File not found", 404

@app.route('/audio_segment')
def serve_audio_segment():
    """
    Serves a segment of an audio file, given filename, start, and end times (in seconds).
    The segment is [start-5, end+5] seconds, clipped to file boundaries.
    """
    filename = request.args.get('file')
    start = request.args.get('start', type=float)
    end = request.args.get('end', type=float)
    if not (filename and start is not None and end is not None):
        return "Missing parameters", 400
    if not current_directory:
        return "No directory selected", 400
    audio_path = os.path.join(current_directory, filename)
    if not os.path.exists(audio_path):
        return "Audio file not found", 404
    try:
        audio = AudioSegment.from_file(audio_path)
        duration = len(audio) / 1000.0  # in seconds
        seg_start = max(0, start - 5)
        seg_end = min(duration, end + 5)
        seg_audio = audio[int(seg_start * 1000):int(seg_end * 1000)]
        # Write to a temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as tmpf:
            seg_audio.export(tmpf.name, format='wav')
            tmpf.flush()
            return send_file(tmpf.name, mimetype='audio/wav', as_attachment=False, download_name=f'{filename}_segment.wav')
    except Exception as e:
        app.logger.error(f"Error extracting audio segment: {e}")
        return f"Error extracting audio segment: {str(e)}", 500

@app.route('/')
def index():
    """
    Renders the main HTML page for the client-side audio player.
    """
    return render_template('index.html') 

@app.route('/select_directory', methods=['POST'])
def select_directory():
    """
    Handles the selection of an audio directory and populates the playlist.
    Now also includes transcription data if a log file has been uploaded/loaded.
    """
    global current_directory, current_playlist, audio_file_map

    directory_path = request.form.get('directory_path')

    if not directory_path:
        return jsonify({"success": False, "message": "Please enter a directory path."})

    if not os.path.isdir(directory_path):
        return jsonify({"success": False, "message": "Invalid directory path."})

    current_directory = directory_path
    current_playlist = []
    audio_file_map = {}
    
    # Recursively load all audio files from the directory and subdirectories
    for root, _, files in os.walk(directory_path):
        for filename in files:
            if filename.lower().endswith(SUPPORTED_AUDIO_EXTENSIONS):
                full_path = os.path.join(root, filename)
                current_playlist.append(full_path)
                # Map just the filename to the full path for CSV lookups
                audio_file_map[filename] = full_path
                # Also map filename without extension for CSV compatibility
                # (CSV has "ac083_2008-04-06" but file is "ac083_2008-04-06.mp3")
                name_without_ext = os.path.splitext(filename)[0]
                audio_file_map[name_without_ext] = full_path
    
    current_playlist.sort()
    
    files_with_transcription_info = []

    for f_path in current_playlist:
        f_name = os.path.basename(f_path)
        file_url = f'/audio_files/{f_name}'
        error_segments = parsed_transcription_data.get(f_name, [])
        files_with_transcription_info.append({
            "name": f_name,
            "url": file_url,
            "error_segments": error_segments
        })

    return jsonify({
        "success": True,
        "message": f"Directory selected: {directory_path}",
        "files_with_info": files_with_transcription_info
    })

@app.route('/upload_log', methods=['POST'])
def upload_log():
    """
    Handles the upload of the log file directly from the client.
    """
    global parsed_transcription_data
    
    log_content = None

    if 'log_file' in request.files:
        uploaded_file = request.files['log_file']
        if uploaded_file.filename != '':
            log_content = uploaded_file.read().decode('utf-8')
    
    if not log_content:
        return jsonify({"success": False, "message": "No log file provided."})

    try:
        parsed_transcription_data = parse_log_content(log_content)
        # Re-select directory to refresh playlist with new transcription data
        if current_directory:
            pass # The frontend will call selectDirectory() after a successful upload
        return jsonify({
            "success": True,
            "message": f"Log file uploaded and parsed successfully. {len(parsed_transcription_data)} entries found."
        })
    except Exception as e:
        app.logger.error(f"Error parsing uploaded log file: {e}")
        return jsonify({"success": False, "message": f"Error parsing uploaded log file: {str(e)}"})

@app.route('/load_log_from_path', methods=['POST'])
def load_log_from_path():
    """
    Handles loading the log file from a specified path on the server system.
    """
    global parsed_transcription_data
    
    log_file_path = request.form.get('log_path')

    if not log_file_path:
        return jsonify({"success": False, "message": "No log file path provided."})
    
    if not os.path.exists(log_file_path):
        return jsonify({"success": False, "message": f"Log file not found at path: {log_file_path}"})
    
    if not os.path.isfile(log_file_path):
        return jsonify({"success": False, "message": f"Provided path is not a file: {log_file_path}"})

    try:
        with open(log_file_path, 'r', encoding='utf-8') as f:
            log_content = f.read()
        
        parsed_transcription_data = parse_log_content(log_content)
        # Re-select directory to refresh playlist with new transcription data
        if current_directory:
            pass # The frontend will call selectDirectory() after a successful load
        return jsonify({
            "success": True,
            "message": f"Log file loaded from path and parsed successfully. {len(parsed_transcription_data)} entries found."
        })
    except Exception as e:
        app.logger.error(f"Error loading or parsing log file from path '{log_file_path}': {e}")
        return jsonify({"success": False, "message": f"Error loading or parsing log file: {str(e)}"})


@app.route('/status', methods=['GET'])
def get_status():
    """
    Returns the current status of the player, including directory and playlist info.
    Now also indicates if a log file has been loaded and includes transcription data.
    """
    files_with_transcription_info = []
    # If a directory has been selected, build the current playlist data for status
    if current_directory:
        for f_path in current_playlist:
            f_name = os.path.basename(f_path)
            file_url = f'/audio_files/{f_name}'
            error_segments = parsed_transcription_data.get(f_name, [])
            files_with_transcription_info.append({
                "name": f_name,
                "url": file_url,
                "error_segments": error_segments
            })

    return jsonify({
        "currentDirectory": current_directory,
        "files_with_info": files_with_transcription_info,
        "logLoaded": bool(parsed_transcription_data),
        "csvLoaded": csv_file_loaded,
        "csvRecordCount": len(csv_error_data)
    })


# Error labeling routes

@app.route('/labeling')
def labeling():
    """Serve the labeling interface"""
    return render_template('labeling.html')


@app.route('/load_csv', methods=['POST'])
def load_csv():
    """Load CSV error data from file upload or path"""
    global csv_error_data, csv_file_loaded
    
    try:
        csv_content = None
        
        # Check if file was uploaded
        if 'csv_file' in request.files:
            file = request.files['csv_file']
            if file and file.filename:
                csv_content = file.read().decode('utf-8')
        
        # Check if path was provided
        elif 'csv_path' in request.form:
            csv_path = request.form['csv_path'].strip()
            if csv_path and os.path.exists(csv_path):
                with open(csv_path, 'r', encoding='utf-8') as f:
                    csv_content = f.read()
            else:
                return jsonify({"success": False, "message": f"CSV file not found at path: {csv_path}"})
        
        if not csv_content:
            return jsonify({"success": False, "message": "No CSV file provided"})
        
        # Parse CSV content
        csv_error_data = []
        reader = csv.DictReader(StringIO(csv_content))
        
        for row in reader:
            # Map actual CSV columns to expected format
            if all(col in row for col in ['recordErrorID', 'recordFile', 'exampleExample', 'recordTime']):
                # Skip rows where recordTime is empty or invalid
                try:
                    record_time = time_to_seconds(row['recordTime']) if row['recordTime'].strip() else 0.0
                    record_file = row['recordFile'].strip()
                    
                    # Debug logging
                    app.logger.info(f"Processing CSV row: ID={row['recordErrorID']}, File='{record_file}', Time={record_time}")
                    
                    # Skip rows with empty filenames
                    if not record_file:
                        app.logger.warning(f"Skipping row {row['recordErrorID']} - empty filename")
                        continue
                    
                    csv_error_data.append({
                        'record_id': row['recordErrorID'],
                        'record_file': record_file,
                        'example_phrase': row['exampleExample'],
                        'record_time': record_time
                    })
                except (ValueError, AttributeError) as e:
                    app.logger.warning(f"Skipping invalid row: {e}")
                    continue  # Skip rows with invalid time values
        
        csv_file_loaded = True
        return jsonify({
            "success": True, 
            "message": f"CSV loaded successfully. {len(csv_error_data)} error records found.",
            "record_count": len(csv_error_data)
        })
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Error loading CSV: {str(e)}"})


@app.route('/get_csv_data', methods=['GET'])
def get_csv_data():
    """Return the loaded CSV error data"""
    if not csv_file_loaded:
        return jsonify({"success": False, "message": "No CSV data loaded"})
    
    return jsonify({
        "success": True,
        "data": csv_error_data,
        "count": len(csv_error_data)
    })


@app.route('/save_label', methods=['POST'])
def save_label():
    """Save a labeled error region to CSV file"""
    try:
        data = request.get_json()
        
        # Expected data: record_id, start_time, end_time, audio_file
        if not all(key in data for key in ['record_id', 'start_time', 'end_time', 'audio_file']):
            return jsonify({"success": False, "message": "Missing required fields"})
        
        # Find the error phrase from the original CSV data
        error_phrase = ""
        for error_record in csv_error_data:
            if str(error_record['record_id']) == str(data['record_id']):
                error_phrase = error_record['example_phrase']
                break
        
        # Prepare the labeled data
        label_data = {
            'record_id': data['record_id'],
            'audio_file': data['audio_file'],
            'error_phrase': error_phrase,
            'start_time': round(data['start_time'], 3),
            'end_time': round(data['end_time'], 3),
            'duration': round(data['end_time'] - data['start_time'], 3),
            'labeled_at': __import__('datetime').datetime.now().isoformat()
        }
        
        # Save to CSV file
        output_file = "/opt/data/labeled-segments.csv"
        
        # Check if file exists to determine if we need to write headers
        file_exists = os.path.exists(output_file)
        
        # Ensure the directory exists
        os.makedirs("/opt/data", exist_ok=True)
        
        with open(output_file, 'a', newline='', encoding='utf-8') as csvfile:
            fieldnames = ['record_id', 'audio_file', 'error_phrase', 'start_time', 'end_time', 'duration', 'labeled_at']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            
            # Write header if file is new
            if not file_exists:
                writer.writeheader()
            
            # Write the labeled data
            writer.writerow(label_data)
        
        app.logger.info(f"Label saved to {output_file}: Record {data['record_id']}, "
                       f"Time: {data['start_time']:.3f}-{data['end_time']:.3f}s, "
                       f"File: {data['audio_file']}")
        
        return jsonify({
            "success": True,
            "message": f"Label saved for record {data['record_id']} to {output_file}"
        })
        
    except Exception as e:
        app.logger.error(f"Error saving label: {e}")
        return jsonify({"success": False, "message": f"Error saving label: {str(e)}"})


@app.route('/download_labels')
def download_labels():
    """Download the labeled segments CSV file"""
    output_file = "/opt/data/labeled-segments.csv"
    
    if os.path.exists(output_file):
        return send_file(output_file, as_attachment=True, download_name="labeled-segments.csv")
    else:
        return "No labeled segments file found", 404


@app.route('/view_labels')
def view_labels():
    """View the labeled segments as JSON for display in the interface"""
    output_file = "/opt/data/labeled-segments.csv"
    
    if not os.path.exists(output_file):
        return jsonify({"success": False, "message": "No labeled segments file found", "data": []})
    
    try:
        labeled_segments = []
        with open(output_file, 'r', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                # Ensure all expected fields exist with defaults
                segment = {
                    'record_id': row.get('record_id', ''),
                    'audio_file': row.get('audio_file', ''),
                    'error_phrase': row.get('error_phrase', ''),  # Handle missing column
                    'start_time': row.get('start_time', '0'),
                    'end_time': row.get('end_time', '0'),
                    'duration': row.get('duration', '0'),
                    'labeled_at': row.get('labeled_at', '')
                }
                labeled_segments.append(segment)
        
        return jsonify({
            "success": True,
            "message": f"Found {len(labeled_segments)} labeled segments",
            "data": labeled_segments
        })
        
    except Exception as e:
        return jsonify({"success": False, "message": f"Error reading labels file: {str(e)}", "data": []})


@app.route('/delete_labels', methods=['POST'])
def delete_labels():
    """Delete the labeled segments file"""
    output_file = "/opt/data/labeled-segments.csv"
    
    try:
        if os.path.exists(output_file):
            os.remove(output_file)
            return jsonify({"success": True, "message": "All labeled segments deleted successfully"})
        else:
            return jsonify({"success": False, "message": "No labeled segments file found to delete"})
    except Exception as e:
        return jsonify({"success": False, "message": f"Error deleting labels file: {str(e)}"})


# Auto-load CSV and audio files on startup
def auto_load_data():
    """Try to auto-load CSV and audio files from default locations"""
    global csv_error_data, csv_file_loaded, current_directory, current_playlist, audio_file_map
    
    # Auto-load audio directory
    audio_path = "/opt/audio"
    if os.path.exists(audio_path) and os.path.isdir(audio_path):
        try:
            current_directory = audio_path
            current_playlist = []
            audio_file_map = {}
            
            # Recursively load all audio files
            for root, _, files in os.walk(audio_path):
                for filename in files:
                    if filename.lower().endswith(SUPPORTED_AUDIO_EXTENSIONS):
                        full_path = os.path.join(root, filename)
                        current_playlist.append(full_path)
                        # Map full filename and filename without extension
                        audio_file_map[filename] = full_path
                        name_without_ext = os.path.splitext(filename)[0]
                        audio_file_map[name_without_ext] = full_path
            
            current_playlist.sort()
            app.logger.info(f"Auto-loaded {len(current_playlist)} audio files from {audio_path}")
            
        except Exception as e:
            app.logger.error(f"Failed to auto-load audio files: {e}")
    
    # Auto-load CSV file - try both possible names
    csv_path = "/opt/data/err-dataset-orig.csv"
    if not os.path.exists(csv_path):
        csv_path = "/opt/data/err-dataset.csv"
    if os.path.exists(csv_path):
        try:
            with open(csv_path, 'r', encoding='utf-8') as f:
                csv_content = f.read()
            
            csv_error_data = []
            reader = csv.DictReader(StringIO(csv_content))
            
            for row in reader:
                if all(col in row for col in ['recordErrorID', 'recordFile', 'exampleExample', 'recordTime']):
                    try:
                        record_time = time_to_seconds(row['recordTime']) if row['recordTime'].strip() else 0.0
                        record_file = row['recordFile'].strip()
                        
                        if record_file:  # Skip empty filenames
                            csv_error_data.append({
                                'record_id': row['recordErrorID'],
                                'record_file': record_file,
                                'example_phrase': row['exampleExample'],
                                'record_time': record_time
                            })
                    except (ValueError, AttributeError):
                        continue  # Skip rows with invalid time values
            
            csv_file_loaded = True
            app.logger.info(f"Auto-loaded CSV with {len(csv_error_data)} error records")
            
        except Exception as e:
            app.logger.error(f"Failed to auto-load CSV: {e}")


if __name__ == '__main__':
    # Auto-load CSV and audio files on startup
    auto_load_data()
    app.run(debug=True, host='0.0.0.0', port=3000)
