from flask import Flask, request, jsonify, send_from_directory
import os
import asyncio
import requests
import threading
import json
import re
import logging
import time
import uuid
from urllib.parse import urlparse, urljoin
from crawl4ai import AsyncWebCrawler

app = Flask(__name__, static_folder='static')

# Create static directory if it doesn't exist
os.makedirs('static/docs', exist_ok=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Store ongoing crawl tasks and results
active_crawls = {}
crawl_results_cache = {}  # Cache for storing crawl results
MAX_CACHE_SIZE = 100  # Maximum number of results to keep in cache

async def crawl_website(url, config):
    """
    Perform website crawling with advanced configuration options
    
    Args:
        url: Target URL to crawl
        config: Dictionary containing crawl configuration
    """
    # Extract configuration parameters
    depth = config.get('depth', 1)
    max_pages = config.get('max_pages', 100)
    excluded_domains = config.get('excluded_domains', [])
    include_only_domains = config.get('include_only_domains', [])
    extract_images = config.get('extract_images', False)
    css_selectors = config.get('css_selectors', None)
    follow_external_links = config.get('follow_external_links', False)
    page_interactions = config.get('page_interactions', [])
    wait_for_selectors = config.get('wait_for_selectors', [])
    extract_metadata = config.get('extract_metadata', False)
    media_download = config.get('media_download', False)
    media_types = config.get('media_types', ['image'])
    user_agent = config.get('user_agent', None)
    timeout = config.get('timeout', 30)
    rate_limit = config.get('rate_limit', 0)  # Requests per second (0 = no limit)
    
    crawler_options = {
        'depth': depth,
        'max_pages': max_pages,
        'follow_external_links': follow_external_links,
        'timeout': timeout
    }
    
    # Add custom options for the crawler
    if css_selectors:
        crawler_options['css_selectors'] = css_selectors
    
    if user_agent:
        crawler_options['user_agent'] = user_agent
        
    if rate_limit > 0:
        crawler_options['rate_limit'] = rate_limit
        
    if wait_for_selectors:
        crawler_options['wait_for_selectors'] = wait_for_selectors
    
    async with AsyncWebCrawler(**crawler_options) as crawler:
        # Apply domain filtering if specified
        if excluded_domains:
            crawler.exclude_domains(excluded_domains)
        if include_only_domains:
            crawler.include_only_domains(include_only_domains)
            
        # Configure page interactions if specified
        if page_interactions:
            for interaction in page_interactions:
                action_type = interaction.get('type')
                selector = interaction.get('selector')
                value = interaction.get('value', None)
                
                if action_type and selector:
                    crawler.add_interaction(action_type, selector, value)
        
        # Run the crawler with rate limiting if specified
        start_time = time.time()
        result = await crawler.arun(url=url)
        crawl_time = time.time() - start_time
        
        # Process the results
        response_data = {
            "url": url,
            "depth": depth,
            "content": result.markdown,
            "crawl_time": crawl_time,
            "pages_crawled": len(result.pages) if hasattr(result, 'pages') else 1
        }
        
        # Extract links if available in the result
        if hasattr(result, 'links') and result.links:
            internal_links = []
            external_links = []
            base_domain = urlparse(url).netloc
            
            for link in result.links:
                link_domain = urlparse(link).netloc
                if link_domain == base_domain or not link_domain:
                    internal_links.append(link)
                else:
                    external_links.append(link)
            
            # Group links by domain for better analysis
            domains = {}
            for link in result.links:
                link_domain = urlparse(link).netloc
                if link_domain:
                    domains[link_domain] = domains.get(link_domain, 0) + 1
                    
            response_data["links"] = {
                "internal": internal_links,
                "external": external_links,
                "domains": domains,
                "total_count": len(result.links)
            }
        
        # Extract media content if requested
        if extract_images and hasattr(result, 'images'):
            images_data = []
            for img in result.images:
                if isinstance(img, str):
                    # Simple URL string
                    image_info = {"url": img}
                else:
                    # Object with metadata
                    image_info = {
                        "url": img.get("src", ""),
                        "alt": img.get("alt", ""),
                        "width": img.get("width", ""),
                        "height": img.get("height", "")
                    }
                    
                # Add absolute URL if it's relative
                if image_info["url"] and not image_info["url"].startswith(('http://', 'https://')):
                    image_info["url"] = urljoin(url, image_info["url"])
                    
                images_data.append(image_info)
                
            response_data["images"] = images_data
            
        # Extract other media types if requested
        if media_download and hasattr(result, 'media'):
            media_data = {}
            for media_type in media_types:
                if media_type in result.media:
                    media_data[media_type] = result.media[media_type]
            
            if media_data:
                response_data["media"] = media_data
                
        # Extract metadata if requested
        if extract_metadata and hasattr(result, 'metadata'):
            response_data["metadata"] = result.metadata
            
        return response_data

def send_callback(callback_url, data):
    """Send crawl results to the specified callback URL"""
    try:
        requests.post(
            callback_url,
            json=data,
            headers={"Content-Type": "application/json"}
        )
        logger.info(f"Callback sent to {callback_url}")
    except Exception as e:
        logger.error(f"Callback failed: {str(e)}")

def process_crawl_async(task_id, url, config, callback_url=None):
    """Process crawl in a separate thread and send callback if provided"""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    try:
        result = loop.run_until_complete(crawl_website(url, config))
        active_crawls[task_id]["status"] = "completed"
        active_crawls[task_id]["result"] = result
        active_crawls[task_id]["completed_at"] = time.time()
        
        # Store in cache for future retrieval
        crawl_results_cache[task_id] = {
            "result": result,
            "timestamp": time.time()
        }
        
        # Clean up cache if it gets too large
        if len(crawl_results_cache) > MAX_CACHE_SIZE:
            # Remove oldest entries
            oldest_keys = sorted(crawl_results_cache.keys(), 
                                key=lambda k: crawl_results_cache[k]["timestamp"])[:len(crawl_results_cache) - MAX_CACHE_SIZE]
            for key in oldest_keys:
                del crawl_results_cache[key]
        
        # Send callback if URL is provided
        if callback_url:
            send_callback(callback_url, {
                "task_id": task_id,
                "status": "completed",
                "result": result
            })
    except Exception as e:
        error_message = str(e)
        active_crawls[task_id]["status"] = "failed"
        active_crawls[task_id]["error"] = error_message
        logger.error(f"Crawl failed for {url}: {error_message}")
        
        if callback_url:
            send_callback(callback_url, {
                "task_id": task_id,
                "status": "failed",
                "error": error_message
            })
    finally:
        loop.close()

@app.route('/crawl', methods=['GET', 'POST'])
def crawl():
    """
    Endpoint to initiate website crawling
    
    GET parameters:
    - url: Target URL to crawl
    - depth: Crawl depth (default: 1)
    - synchronous: If "true", wait for crawl to complete (default: "false")
    
    POST parameters (JSON):
    - url: Target URL to crawl
    - depth: Crawl depth (default: 1)
    - max_pages: Maximum pages to crawl (default: 100)
    - excluded_domains: List of domains to exclude
    - include_only_domains: List of domains to exclusively include
    - extract_images: Whether to extract images (default: false)
    - css_selectors: CSS selectors to extract specific content
    - follow_external_links: Whether to follow external links (default: false)
    - callback_url: URL to send results when crawl completes
    - synchronous: If true, wait for crawl to complete (default: false)
    - page_interactions: List of interactions to perform on pages
    - wait_for_selectors: CSS selectors to wait for before considering page loaded
    - extract_metadata: Whether to extract page metadata (default: false)
    - media_download: Whether to download media files (default: false)
    - media_types: Types of media to download (default: ['image'])
    - user_agent: Custom user agent string
    - timeout: Request timeout in seconds (default: 30)
    - rate_limit: Maximum requests per second (default: 0, no limit)
    """
    # Get parameters from either GET or POST request
    if request.method == 'GET':
        url = request.args.get('url')
        config = {
            'depth': int(request.args.get('depth', 1)),
            'max_pages': int(request.args.get('max_pages', 100))
        }
        synchronous = request.args.get('synchronous', 'false').lower() == 'true'
        callback_url = request.args.get('callback_url')
    else:  # POST
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid JSON data"}), 400
            
        url = data.get('url')
        config = {
            'depth': int(data.get('depth', 1)),
            'max_pages': int(data.get('max_pages', 100)),
            'excluded_domains': data.get('excluded_domains', []),
            'include_only_domains': data.get('include_only_domains', []),
            'extract_images': data.get('extract_images', False),
            'css_selectors': data.get('css_selectors'),
            'follow_external_links': data.get('follow_external_links', False),
            'page_interactions': data.get('page_interactions', []),
            'wait_for_selectors': data.get('wait_for_selectors', []),
            'extract_metadata': data.get('extract_metadata', False),
            'media_download': data.get('media_download', False),
            'media_types': data.get('media_types', ['image']),
            'user_agent': data.get('user_agent'),
            'timeout': int(data.get('timeout', 30)),
            'rate_limit': float(data.get('rate_limit', 0))
        }
        synchronous = data.get('synchronous', False)
        callback_url = data.get('callback_url')

    if not url:
        return jsonify({"error": "URL parameter is missing"}), 400

    # For synchronous requests, run the crawl and return results immediately
    if synchronous:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(crawl_website(url, config))
            return jsonify(result)
        finally:
            loop.close()
    
    # For asynchronous requests, create a task and return task ID
    task_id = f"task_{uuid.uuid4()}"
    active_crawls[task_id] = {
        "url": url,
        "config": config,
        "status": "running",
        "callback_url": callback_url
    }
    
    # Start crawling in a separate thread
    thread = threading.Thread(
        target=process_crawl_async,
        args=(task_id, url, config, callback_url)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({
        "task_id": task_id,
        "status": "running",
        "message": "Crawl started asynchronously"
    })

@app.route('/status/<task_id>', methods=['GET'])
def get_status(task_id):
    """Get the status of an ongoing or completed crawl task"""
    if task_id not in active_crawls:
        return jsonify({"error": "Task not found"}), 404
        
    task_info = active_crawls[task_id]
    response = {
        "task_id": task_id,
        "url": task_info["url"],
        "status": task_info["status"]
    }
    
    # Include result if completed
    if task_info["status"] == "completed" and "result" in task_info:
        response["result"] = task_info["result"]
    
    # Include error if failed
    if task_info["status"] == "failed" and "error" in task_info:
        response["error"] = task_info["error"]
        
    return jsonify(response)

@app.route('/tasks', methods=['GET'])
def list_tasks():
    """List all active and recently completed crawl tasks"""
    # Filter tasks based on status if specified
    status_filter = request.args.get('status')
    
    tasks = {}
    for task_id, task_info in active_crawls.items():
        if status_filter and task_info.get('status') != status_filter:
            continue
            
        # Include basic task info without full results
        tasks[task_id] = {
            "url": task_info.get("url"),
            "status": task_info.get("status"),
            "created_at": task_info.get("created_at", 0),
            "completed_at": task_info.get("completed_at", 0) if task_info.get("status") == "completed" else None
        }
    
    return jsonify({
        "tasks": tasks,
        "count": len(tasks)
    })

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_task(task_id):
    """Cancel an ongoing crawl task"""
    if task_id not in active_crawls:
        return jsonify({"error": "Task not found"}), 404
        
    task_info = active_crawls[task_id]
    if task_info["status"] != "running":
        return jsonify({"error": "Task is not running"}), 400
        
    # Mark task as cancelled
    active_crawls[task_id]["status"] = "cancelled"
    
    return jsonify({
        "task_id": task_id,
        "status": "cancelled",
        "message": "Task cancellation requested"
    })

@app.route('/batch', methods=['POST'])
def batch_crawl():
    """
    Start multiple crawl tasks in a single request
    
    POST parameters (JSON):
    - urls: List of URLs to crawl
    - config: Configuration to apply to all crawls
    - callback_url: URL to send results when all crawls complete
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    urls = data.get('urls', [])
    if not urls:
        return jsonify({"error": "No URLs provided"}), 400
        
    config = data.get('config', {})
    callback_url = data.get('callback_url')
    
    # Create a batch ID
    batch_id = f"batch_{uuid.uuid4()}"
    task_ids = []
    
    # Start each crawl task
    for url in urls:
        task_id = f"task_{uuid.uuid4()}"
        task_ids.append(task_id)
        
        active_crawls[task_id] = {
            "url": url,
            "config": config,
            "status": "running",
            "callback_url": callback_url,
            "batch_id": batch_id,
            "created_at": time.time()
        }
        
        # Start crawling in a separate thread
        thread = threading.Thread(
            target=process_crawl_async,
            args=(task_id, url, config, callback_url)
        )
        thread.daemon = True
        thread.start()
    
    return jsonify({
        "batch_id": batch_id,
        "task_ids": task_ids,
        "status": "running",
        "message": f"Started {len(urls)} crawl tasks"
    })

@app.route('/batch/<batch_id>', methods=['GET'])
def batch_status(batch_id):
    """Get the status of all tasks in a batch"""
    batch_tasks = {}
    completed_count = 0
    
    for task_id, task_info in active_crawls.items():
        if task_info.get("batch_id") == batch_id:
            task_data = {
                "url": task_info.get("url"),
                "status": task_info.get("status")
            }
            
            # Include result for completed tasks
            if task_info.get("status") == "completed" and "result" in task_info:
                task_data["result"] = task_info["result"]
            
            # Include error for failed tasks
            if task_info.get("status") == "failed" and "error" in task_info:
                task_data["error"] = task_info["error"]
                
            batch_tasks[task_id] = task_data
            
            if task_info.get("status") in ["completed", "failed", "cancelled"]:
                completed_count += 1
    
    if not batch_tasks:
        return jsonify({"error": "Batch not found"}), 404
        
    return jsonify({
        "batch_id": batch_id,
        "tasks": batch_tasks,
        "total": len(batch_tasks),
        "completed": completed_count,
        "status": "completed" if completed_count == len(batch_tasks) else "running"
    })

@app.route('/health', methods=['GET'])
def health_check():
    """API health check endpoint"""
    try:
        import psutil
        process = psutil.Process()
        memory_info = process.memory_info()
        memory_usage_mb = memory_info.rss / 1024 / 1024
        cpu_usage_percent = process.cpu_percent(interval=0.1)
        
        # Get disk usage
        disk = psutil.disk_usage('/')
        disk_usage_percent = disk.percent
    except ImportError:
        memory_usage_mb = 0
        cpu_usage_percent = 0
        disk_usage_percent = 0
    
    # Calculate average crawl time from completed tasks
    completed_tasks = [t for t in active_crawls.values() if t.get("status") == "completed" and "result" in t]
    total_crawl_time = sum(t.get("result", {}).get("crawl_time", 0) for t in completed_tasks)
    avg_crawl_time = round(total_crawl_time / len(completed_tasks), 2) if completed_tasks else 0
    
    stats = {
        "active_tasks": len([t for t in active_crawls.values() if t.get("status") == "running"]),
        "completed_tasks": len([t for t in active_crawls.values() if t.get("status") == "completed"]),
        "failed_tasks": len([t for t in active_crawls.values() if t.get("status") == "failed"]),
        "cancelled_tasks": len([t for t in active_crawls.values() if t.get("status") == "cancelled"]),
        "cached_results": len(crawl_results_cache),
        "uptime": time.time() - app.start_time if hasattr(app, 'start_time') else 0,
        "memory_usage_mb": round(memory_usage_mb, 1),
        "cpu_usage_percent": round(cpu_usage_percent, 1),
        "disk_usage_percent": round(disk_usage_percent, 1),
        "queue_size": 0,  # Placeholder for task queue if implemented
        "average_crawl_time": avg_crawl_time
    }
    
    return jsonify({
        "status": "healthy",
        "stats": stats
    })

@app.route('/docs/api', methods=['GET'])
def api_docs():
    """Serve the API documentation HTML page"""
    return send_from_directory('.', 'api_documentation.html')

@app.route('/docs/directus', methods=['GET'])
def directus_docs():
    """Serve the Directus integration tutorial HTML page"""
    return send_from_directory('.', 'directus_tutorial.html')

@app.route('/docs/directus-appsmith', methods=['GET'])
def directus_appsmith_docs():
    """Serve the Directus + Appsmith integration tutorial HTML page"""
    return send_from_directory('.', 'directus_appsmith_tutorial.html')

@app.route('/docs/crawlai-directus-appsmith', methods=['GET'])
def crawlai_directus_appsmith_docs():
    """Serve the CrawlAI + Directus + Appsmith integration tutorial HTML page"""
    return send_from_directory('.', 'crawlai_directus_appsmith_tutorial.html')

if __name__ == '__main__':
    app.start_time = time.time()
    app.run(host='0.0.0.0', port=5000)

