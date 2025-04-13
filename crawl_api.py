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
import http.client
import ssl
import sys
from urllib.parse import urlparse, urljoin
from crawl4ai import AsyncWebCrawler, CrawlerRunConfig, LLMConfig
from crawl4ai.extraction_strategy import JsonCssExtractionStrategy, LLMExtractionStrategy
from crawl_api_backups.convert_to_article import convert_to_article
try:
    from bs4 import BeautifulSoup
except ImportError:
    logging.warning("BeautifulSoup not installed. Manual HTML parsing will be limited.")

app = Flask(__name__, static_folder='static')

# Create static directory if it doesn't exist
os.makedirs('static/docs', exist_ok=True)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Increase recursion limit to handle deeper nested structures
sys.setrecursionlimit(3000)

# Store ongoing crawl tasks and results
active_crawls = {}
crawl_results_cache = {}  # Cache for storing crawl results
MAX_CACHE_SIZE = 100  # Maximum number of results to keep in cache

def make_json_serializable(obj, max_depth=10, current_depth=0):
    """
    Recursively convert an object to a JSON-serializable format,
    handling circular references and limiting recursion depth.
    """
    if current_depth >= max_depth:
        return "MAX_DEPTH_REACHED"
    
    # Handle basic types that are already serializable
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    
    # Handle lists
    if isinstance(obj, list):
        return [make_json_serializable(item, max_depth, current_depth + 1) for item in obj]
    
    # Handle dictionaries
    if isinstance(obj, dict):
        return {str(k): make_json_serializable(v, max_depth, current_depth + 1) for k, v in obj.items()}
    
    # Handle objects with __dict__ attribute (convert to dict)
    if hasattr(obj, '__dict__'):
        return make_json_serializable(obj.__dict__, max_depth, current_depth + 1)
    
    # Handle other iterables
    try:
        if hasattr(obj, '__iter__') and not isinstance(obj, (str, bytes, bytearray)):
            return [make_json_serializable(item, max_depth, current_depth + 1) for item in obj]
    except:
        pass
    
    # For anything else, convert to string
    try:
        return str(obj)
    except:
        return "UNSERIALIZABLE_OBJECT"

def debug_result_object(result, logger, max_depth=2, current_depth=0):
    """Debug helper to log details about the result object with depth limiting"""
    if current_depth >= max_depth:
        logger.info(f"Reached maximum inspection depth ({max_depth})")
        return
    
    logger.info(f"Result type: {type(result)}")
    logger.info(f"Result attributes: {dir(result)}")
    
    # Check for common attributes and log their types/values
    for attr in ['cleaned_html', 'html', 'extracted_content', 'structured_data', 
                'content', 'markdown', 'links', 'images', 'media', 'success', 'error_message']:
        if hasattr(result, attr):
            value = getattr(result, attr)
            if value:
                if isinstance(value, str):
                    logger.info(f"Attribute '{attr}' is string with length: {len(value)}")
                elif isinstance(value, list):
                    logger.info(f"Attribute '{attr}' is list with {len(value)} items")
                    # Don't log list contents to avoid recursion
                elif isinstance(value, dict):
                    logger.info(f"Attribute '{attr}' is dict with keys: {list(value.keys())}")
                    # Don't log dict values to avoid recursion
                else:
                    logger.info(f"Attribute '{attr}' is {type(value)}")
                    
                    # Special handling for markdown to check if it's a MarkdownGenerationResult
                    if attr == 'markdown' and not isinstance(value, str) and current_depth < max_depth - 1:
                        logger.info(f"Markdown object attributes: {dir(value)}")
                        for md_attr in ['raw_markdown', 'markdown_with_citations', 'references_markdown', 'fit_markdown', 'fit_html']:
                            if hasattr(value, md_attr) and getattr(value, md_attr):
                                md_value = getattr(value, md_attr)
                                if isinstance(md_value, str):
                                    logger.info(f"Markdown.{md_attr} exists with type: {type(md_value)} and length: {len(md_value)}")
                                else:
                                    logger.info(f"Markdown.{md_attr} exists with type: {type(md_value)}")
            else:
                logger.info(f"Attribute '{attr}' exists but is empty")

async def crawl_website(url, config):
    """
    Perform website crawling with advanced configuration options
    
    Args:
        url: Target URL to crawl
        config: Dictionary containing crawl configuration
    """
    # Log the start of crawl with URL
    logger.info(f"Starting crawl of URL: {url}")
    
    # Extract configuration parameters with safe defaults
    try:
        depth = int(config.get('depth', 1))
    except (ValueError, TypeError):
        depth = 1
        logger.warning(f"Invalid depth value, using default: {depth}")
        
    try:
        max_pages = int(config.get('max_pages', 100))
    except (ValueError, TypeError):
        max_pages = 100
        logger.warning(f"Invalid max_pages value, using default: {max_pages}")
        
    excluded_domains = config.get('excluded_domains', [])
    include_only_domains = config.get('include_only_domains', [])
    extract_images = config.get('extract_images', False)
    css_selector = config.get('css_selector', None)
    target_elements = config.get('target_elements', None)
    follow_external_links = config.get('follow_external_links', False)
    page_interactions = config.get('page_interactions', [])
    wait_for_selectors = config.get('wait_for_selectors', [])
    extract_metadata = config.get('extract_metadata', False)
    media_download = config.get('media_download', False)
    media_types = config.get('media_types', ['image'])
    user_agent = config.get('user_agent', None)
    
    try:
        timeout = int(config.get('timeout', 30))
    except (ValueError, TypeError):
        timeout = 30
        logger.warning(f"Invalid timeout value, using default: {timeout}")
        
    try:
        rate_limit = float(config.get('rate_limit', 0))  # Requests per second (0 = no limit)
    except (ValueError, TypeError):
        rate_limit = 0
        logger.warning(f"Invalid rate_limit value, using default: {rate_limit}")
    
    crawler_options = {
        'depth': depth,
        'max_pages': max_pages,
        'follow_external_links': follow_external_links,
        'timeout': timeout
    }
    
    # Create a CrawlerRunConfig object with no parameters
    # All parameters will be set as attributes after creation
    crawler_config = CrawlerRunConfig()
    
    # Enable content extraction
    crawler_config.extract_content = True
    
    # Add selectors to the config
    if css_selector:
        logger.info(f"Using CSS selector: {css_selector}")
        crawler_config.css_selector = css_selector
        
    if target_elements:
        logger.info(f"Using target elements: {target_elements}")
        # Convert string to list if it's a single selector
        if isinstance(target_elements, str):
            target_elements = [target_elements]
        # Ensure we're using a list for target_elements
        crawler_config.target_elements = target_elements
    
    # Handle extraction strategy if provided
    extraction_strategy = None
    if config.get('extraction_schema'):
        schema = config.get('extraction_schema')
        logger.info(f"Using extraction schema: {schema}")
        extraction_strategy = JsonCssExtractionStrategy(schema)
        crawler_config.extraction_strategy = extraction_strategy
    elif target_elements or css_selector:
        # Create a default extraction schema for buttons and links
        logger.info("Creating default extraction schema for buttons and links")
        selectors = target_elements if isinstance(target_elements, list) else [target_elements]
        if css_selector and css_selector not in selectors:
            selectors.append(css_selector)
            
        # Filter out None values
        selectors = [s for s in selectors if s]
        
        if selectors:
            # Create a simple schema to extract buttons and links
            schema = {
                "name": "ButtonsAndLinks",
                "baseSelector": "body",  # Start from body
                "fields": [
                    {
                        "name": "elements",
                        "type": "list",
                        "selector": ", ".join(selectors),
                        "fields": [
                            {"name": "text", "type": "text"},
                            {"name": "html", "type": "html"},
                            {"name": "href", "type": "attribute", "attribute": "href"}
                        ]
                    }
                ]
            }
    
            # Also add a simpler schema as fallback
            fallback_schema = {
                "baseSelector": "body",
                "fields": [
                    {"name": "buttons", "selector": "a.btn, button, .wp-block-button__link, a.wp-block-button__link", "type": "list"}
                ]
            }
            logger.info(f"Using auto-generated schema: {schema}")
            try:
                extraction_strategy = JsonCssExtractionStrategy(schema)
                crawler_config.extraction_strategy = extraction_strategy
            except Exception as e:
                logger.warning(f"Error creating extraction strategy: {str(e)}")
                try:
                    logger.info(f"Trying fallback schema: {fallback_schema}")
                    extraction_strategy = JsonCssExtractionStrategy(fallback_schema)
                    crawler_config.extraction_strategy = extraction_strategy
                except Exception as e2:
                    logger.warning(f"Error creating fallback extraction strategy: {str(e2)}")
    
    # Set all parameters as attributes
    crawler_config.depth = depth
    crawler_config.max_pages = max_pages
    crawler_config.follow_external_links = follow_external_links
    crawler_config.debug = config.get('debug', True)  # Enable debug mode by default
    
    # Log the complete configuration
    logger.info(f"Created CrawlerRunConfig with depth={depth}")
    
    # Add all configuration options as attributes
    if excluded_domains:
        crawler_config.excluded_domains = excluded_domains
        
    if include_only_domains:
        crawler_config.include_only_domains = include_only_domains
        
    if extract_images:
        crawler_config.extract_images = extract_images
        
    # word_count_threshold from config
    word_count_threshold = config.get('word_count_threshold', 0)
    if word_count_threshold > 0:
        crawler_config.word_count_threshold = word_count_threshold
        
    # excluded_tags from config
    excluded_tags = config.get('excluded_tags', [])
    if excluded_tags:
        crawler_config.excluded_tags = excluded_tags
        
    if config.get('exclude_external_links', False):
        crawler_config.exclude_external_links = config.get('exclude_external_links')
        
    if config.get('exclude_social_media_links', False):
        crawler_config.exclude_social_media_links = config.get('exclude_social_media_links')
        
    if config.get('exclude_domains', []):
        crawler_config.exclude_domains = config.get('exclude_domains')
        
    if config.get('exclude_external_images', False):
        crawler_config.exclude_external_images = config.get('exclude_external_images')
        
    if config.get('process_iframes', False):
        crawler_config.process_iframes = config.get('process_iframes')
        
    if page_interactions:
        crawler_config.page_interactions = page_interactions
        
    if wait_for_selectors:
        crawler_config.wait_for_selectors = wait_for_selectors
        
    if config.get('extract_metadata', False):
        crawler_config.extract_metadata = config.get('extract_metadata')
        
    if config.get('media_download', False):
        crawler_config.media_download = config.get('media_download')
        
    if config.get('media_types', ['image']):
        crawler_config.media_types = config.get('media_types')
        
    if user_agent:
        crawler_config.user_agent = user_agent
        
    if rate_limit > 0:
        crawler_config.rate_limit = rate_limit
    
    if timeout:
        crawler_config.timeout = timeout
    
    # Log the crawler config for debugging
    logger.info(f"Initializing crawler with config: {vars(crawler_config)}")
    
    async with AsyncWebCrawler() as crawler:
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
        
        # Run the crawler with the config
        start_time = time.time()
        logger.info(f"Starting crawl of {url} with depth {depth}")
        
        # Log the actual method call for debugging
        logger.info(f"Calling crawler.arun with url={url} and config={vars(crawler_config)}")
        result = await crawler.arun(url=url, config=crawler_config)
        
        # Debug the result object to understand its structure with depth limiting
        debug_result_object(result, logger, max_depth=2)
        crawl_time = time.time() - start_time
        logger.info(f"Crawl completed in {crawl_time:.2f} seconds")
        
        # Debug: log available attributes in the result
        logger.info(f"Result object has attributes: {dir(result)}")
        
        # Process the results - make sure everything is JSON serializable
        response_data = {
            "url": url,
            "depth": depth,
            "crawl_time": crawl_time,
            "pages_crawled": len(result.pages) if hasattr(result, 'pages') else 1,
            "extracted_content": [],  # Initialize with empty list to ensure it's always present
            "success": hasattr(result, 'success') and result.success
        }
        
        # Add error message if crawl was not successful
        if hasattr(result, 'success') and not result.success and hasattr(result, 'error_message'):
            response_data["error_message"] = result.error_message
            
        # Handle markdown content based on type
        if hasattr(result, 'markdown'):
            if isinstance(result.markdown, str):
                response_data["content"] = result.markdown
            else:  # It's a MarkdownGenerationResult object
                # Check for raw_markdown first
                if hasattr(result.markdown, 'raw_markdown'):
                    response_data["content"] = result.markdown.raw_markdown
                # Check for markdown with citations
                if hasattr(result.markdown, 'markdown_with_citations'):
                    response_data["content_with_citations"] = result.markdown.markdown_with_citations
                    if hasattr(result.markdown, 'references_markdown'):
                        response_data["references"] = result.markdown.references_markdown
                # Check for fit content if available
                if hasattr(result.markdown, 'fit_markdown') and result.markdown.fit_markdown:
                    response_data["fit_content"] = result.markdown.fit_markdown
                if hasattr(result.markdown, 'fit_html') and result.markdown.fit_html:
                    response_data["fit_html"] = result.markdown.fit_html
        else:
            response_data["content"] = ""
        
        # Debug: Print the entire result object attributes
        logger.info(f"RESULT OBJECT ATTRIBUTES: {dir(result)}")
        
        # Directly check for JSON-formatted extracted_content
        if hasattr(result, 'extracted_content') and result.extracted_content:
            try:
                # If it's a string that might be JSON, try to parse it
                if isinstance(result.extracted_content, str):
                    import json
                    try:
                        parsed_content = json.loads(result.extracted_content)
                        response_data["extracted_content"] = parsed_content
                        logger.info(f"Successfully parsed JSON extracted_content")
                    except json.JSONDecodeError:
                        # Not JSON, use as is
                        response_data["extracted_content"] = result.extracted_content
                        logger.info(f"Using non-JSON extracted_content string")
                else:
                    # Not a string, use as is
                    response_data["extracted_content"] = result.extracted_content
                    logger.info(f"Using non-string extracted_content: {type(result.extracted_content)}")
            except Exception as e:
                logger.warning(f"Error processing extracted_content: {str(e)}")
        
        # Process extracted content from the result
        if hasattr(result, 'extracted_content') and result.extracted_content:
            logger.info(f"Found extracted_content: {type(result.extracted_content)}")
            response_data["extracted_content"] = result.extracted_content
            
            # Log more details if it's a list
            if isinstance(result.extracted_content, list):
                logger.info(f"extracted_content is a list with {len(result.extracted_content)} items")
                if result.extracted_content and len(result.extracted_content) > 0:
                    logger.info(f"First item type: {type(result.extracted_content[0])}")
                    logger.info(f"First item sample: {str(result.extracted_content[0])[:100]}")
        elif hasattr(result, 'cleaned_html') and result.cleaned_html and (css_selector or target_elements):
            # If we used selectors but didn't get extracted_content, use cleaned_html
            logger.info(f"Using cleaned_html as extracted_content, length: {len(result.cleaned_html)}")
            response_data["extracted_content"] = result.cleaned_html
        elif hasattr(result, 'html') and result.html and (css_selector or target_elements):
            # Try to extract content manually using BeautifulSoup
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(result.html, 'html.parser')
                
                extracted_elements = []
                selectors = target_elements if isinstance(target_elements, list) else [target_elements]
                if css_selector and css_selector not in selectors:
                    selectors.append(css_selector)
                
                logger.info(f"Manually extracting content using selectors: {selectors}")
                
                for selector in selectors:
                    if not selector:
                        continue
                    try:
                        elements = soup.select(selector)
                        logger.info(f"Found {len(elements)} elements matching {selector}")
                        
                        for element in elements:
                            extracted_elements.append({
                                "selector": selector,
                                "text": element.get_text(strip=True),
                                "html": str(element),
                                "attributes": {attr: element.get(attr) for attr in element.attrs}
                            })
                    except Exception as e:
                        logger.warning(f"Error with selector '{selector}': {str(e)}")
                
                # If no elements found with specific selectors, try to find buttons
                if not extracted_elements:
                    logger.info("No elements found with specific selectors, trying to find buttons")
                    buttons = soup.find_all(['a', 'button'], class_=lambda c: c and ('btn' in c.lower() or 'button' in c.lower()))
                    logger.info(f"Found {len(buttons)} potential button elements")
                    
                    for button in buttons:
                        extracted_elements.append({
                            "selector": "button-fallback",
                            "text": button.get_text(strip=True),
                            "html": str(button),
                            "attributes": {attr: button.get(attr) for attr in button.attrs}
                        })
                
                if extracted_elements:
                    response_data["extracted_content"] = extracted_elements
                    logger.info(f"Manually extracted {len(extracted_elements)} elements")
                else:
                    logger.warning("No elements found with manual extraction")
            except ImportError:
                logger.warning("BeautifulSoup not available for manual extraction")
                response_data["extracted_content"] = {"error": "BeautifulSoup not available for extraction"}
            except Exception as e:
                logger.warning(f"Error during manual extraction: {str(e)}")
                response_data["extracted_content"] = {"error": f"Manual extraction failed: {str(e)}"}
        
        # If we still don't have any extracted content, provide an empty list
        if not response_data.get("extracted_content") or response_data["extracted_content"] == []:
            logger.warning(f"No extracted content found in result for selectors: {css_selector or target_elements}")
            logger.warning(f"Available attributes: {dir(result)}")
            
            # Last resort: try to extract directly from HTML using BeautifulSoup
            if hasattr(result, 'html') and result.html:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(result.html, 'html.parser')
                    
                    # Look for buttons and links
                    buttons = []
                    for selector in ['.wp-block-button__link', 'a.btn', 'button.btn', '.btn', 'a[class*="btn"]', 'a']:
                        elements = soup.select(selector)
                        if elements:
                            logger.info(f"Found {len(elements)} elements with selector {selector}")
                            for el in elements:
                                # Extract link text, trying different methods
                                link_text = el.get_text(strip=True)
                                if not link_text and el.name == 'a' and el.has_attr('title'):
                                    link_text = el['title']
                                
                                # Get href and base domain
                                href = el.get('href', '') if el.name == 'a' else ''
                                base_domain = urlparse(href).netloc if href else ''
                                
                                buttons.append({
                                    "selector": selector,
                                    "text": link_text,
                                    "html": str(el),
                                    "href": href,
                                    "title": el.get('title', ''),
                                    "base_domain": base_domain
                                })
                    
                    if buttons:
                        response_data["extracted_content"] = buttons
                        logger.info(f"Extracted {len(buttons)} buttons as last resort")
                except Exception as e:
                    logger.warning(f"Last resort extraction failed: {str(e)}")
            
            # If still empty, set to empty list
            if not response_data.get("extracted_content"):
                response_data["extracted_content"] = []
                
        # Add debug info to the response
        if config.get('debug', True):
            response_data["debug_info"] = {
                "result_attributes": dir(result),
                "extraction_config": {
                    "css_selector": css_selector,
                    "target_elements": target_elements,
                    "extraction_strategy": str(extraction_strategy) if extraction_strategy else None
                }
            }
        
        # Extract links if available in the result
        if hasattr(result, 'links'):
            # Check if links is already a dictionary with internal/external keys
            if isinstance(result.links, dict) and "internal" in result.links and "external" in result.links:
                internal_links = []
                external_links = []
                base_domain = urlparse(url).netloc
                
                # Process internal links
                for link in result.links.get("internal", []):
                    if isinstance(link, dict) and 'href' in link:
                        # Already structured properly
                        if 'base_domain' not in link:
                            link_domain = urlparse(link['href']).netloc
                            link['base_domain'] = link_domain
                        internal_links.append(link)
                    else:
                        # Convert string to structured format
                        link_url = link if isinstance(link, str) else link.get('href', '')
                        link_domain = urlparse(link_url).netloc
                        link_text = link.get('text', '') if isinstance(link, dict) else ''
                        link_title = link.get('title', '') if isinstance(link, dict) else ''
                        
                        internal_links.append({
                            "href": link_url,
                            "text": link_text,
                            "title": link_title,
                            "base_domain": link_domain or base_domain
                        })
                
                # Process external links
                for link in result.links.get("external", []):
                    if isinstance(link, dict) and 'href' in link:
                        # Already structured properly
                        if 'base_domain' not in link:
                            link_domain = urlparse(link['href']).netloc
                            link['base_domain'] = link_domain
                        external_links.append(link)
                    else:
                        # Convert string to structured format
                        link_url = link if isinstance(link, str) else link.get('href', '')
                        link_domain = urlparse(link_url).netloc
                        link_text = link.get('text', '') if isinstance(link, dict) else ''
                        link_title = link.get('title', '') if isinstance(link, dict) else ''
                        
                        external_links.append({
                            "href": link_url,
                            "text": link_text,
                            "title": link_title,
                            "base_domain": link_domain
                        })
                
                # Calculate total count
                total_count = len(internal_links) + len(external_links)
                
                # Group links by domain for better analysis
                domains = {}
                for link_list in [internal_links, external_links]:
                    for link in link_list:
                        link_domain = link.get('base_domain', '')
                        if link_domain:
                            domains[link_domain] = domains.get(link_domain, 0) + 1
                
                response_data["links"] = {
                    "internal": internal_links,
                    "external": external_links,
                    "domains": domains,
                    "total_count": total_count
                }
            # Handle case where links is a flat list
            elif isinstance(result.links, list):
                internal_links = []
                external_links = []
                base_domain = urlparse(url).netloc
                
                for link in result.links:
                    # Determine if it's internal or external
                    if isinstance(link, dict) and 'href' in link:
                        link_url = link['href']
                    else:
                        link_url = link if isinstance(link, str) else ''
                    
                    link_domain = urlparse(link_url).netloc
                    is_internal = link_domain == base_domain or not link_domain
                    
                    # Create structured link object
                    structured_link = {}
                    if isinstance(link, dict):
                        structured_link = {
                            "href": link.get('href', ''),
                            "text": link.get('text', ''),
                            "title": link.get('title', ''),
                            "base_domain": link_domain or base_domain
                        }
                    else:
                        structured_link = {
                            "href": link_url,
                            "text": "",
                            "title": "",
                            "base_domain": link_domain or base_domain
                        }
                    
                    # Add to appropriate list
                    if is_internal:
                        internal_links.append(structured_link)
                    else:
                        external_links.append(structured_link)
                
                # Group links by domain for better analysis
                domains = {}
                for link_list in [internal_links, external_links]:
                    for link in link_list:
                        link_domain = link.get('base_domain', '')
                        if link_domain:
                            domains[link_domain] = domains.get(link_domain, 0) + 1
                        
                response_data["links"] = {
                    "internal": internal_links,
                    "external": external_links,
                    "domains": domains,
                    "total_count": len(result.links)
                }
        
        # Extract media content if requested
        if extract_images:
            images_data = []
            
            # Check if media dictionary exists with images key
            if hasattr(result, 'media') and isinstance(result.media, dict) and "images" in result.media:
                for img in result.media["images"]:
                    if isinstance(img, str):
                        # Simple URL string
                        image_info = {"url": img}
                    else:
                        # Object with metadata
                        image_info = {
                            "url": img.get("src", ""),
                            "alt": img.get("alt", ""),
                            "width": img.get("width", ""),
                            "height": img.get("height", ""),
                            "score": img.get("score", 0)
                        }
                        
                    # Add absolute URL if it's relative
                    if image_info["url"] and not image_info["url"].startswith(('http://', 'https://')):
                        image_info["url"] = urljoin(url, image_info["url"])
                        
                    images_data.append(image_info)
            # Fallback to legacy images attribute if it exists
            elif hasattr(result, 'images'):
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
                
            if images_data:
                response_data["images"] = images_data
            
        # Extract all media types if requested
        if media_download and hasattr(result, 'media') and isinstance(result.media, dict):
            media_data = {}
            for media_type in media_types:
                if media_type in result.media:
                    media_data[media_type] = result.media[media_type]
            
            # Add videos and audios if they exist but weren't explicitly requested
            for additional_type in ["videos", "audios"]:
                if additional_type in result.media and additional_type not in media_data:
                    media_data[additional_type] = result.media[additional_type]
            
            if media_data:
                response_data["media"] = media_data
                
        # Extract metadata if requested or available
        if (extract_metadata or config.get('extract_metadata', False)) and hasattr(result, 'metadata'):
            response_data["metadata"] = result.metadata
            
        # Add status code if available
        if hasattr(result, 'status_code'):
            response_data["status_code"] = result.status_code
            
        # Add response headers if available
        if hasattr(result, 'response_headers'):
            response_data["response_headers"] = result.response_headers
            
        # Add SSL certificate info if available
        if hasattr(result, 'ssl_certificate'):
            response_data["ssl_certificate"] = {
                "issuer": result.ssl_certificate.issuer if hasattr(result.ssl_certificate, 'issuer') else None,
                "subject": result.ssl_certificate.subject if hasattr(result.ssl_certificate, 'subject') else None,
                "valid_from": result.ssl_certificate.valid_from if hasattr(result.ssl_certificate, 'valid_from') else None,
                "valid_until": result.ssl_certificate.valid_until if hasattr(result.ssl_certificate, 'valid_until') else None
            }
            
        # Make sure the response is fully JSON serializable before returning
        return make_json_serializable(response_data, max_depth=8)

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
        
        # Store in cache for future retrieval - ensure it's JSON serializable
        crawl_results_cache[task_id] = {
            "result": make_json_serializable(result, max_depth=5),
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
    - css_selector: CSS selector to extract specific content
    - target_elements: List of CSS selectors to focus on for content extraction
    - extraction_schema: JSON schema for structured extraction
    - word_count_threshold: Minimum words per block to include
    - excluded_tags: HTML tags to exclude from extraction
    - exclude_external_links: Whether to exclude external links
    - exclude_social_media_links: Whether to exclude social media links
    - exclude_domains: List of domains to exclude
    - exclude_external_images: Whether to exclude external images
    - process_iframes: Whether to process iframe content
    - debug: Enable debug mode for more detailed logging (default: true)
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
        
        # Safe conversion of numeric parameters with error handling
        try:
            depth = int(data.get('depth', 1))
        except (ValueError, TypeError):
            depth = 1
            
        try:
            max_pages = int(data.get('max_pages', 100))
        except (ValueError, TypeError):
            max_pages = 100
            
        try:
            timeout = int(data.get('timeout', 30))
        except (ValueError, TypeError):
            timeout = 30
            
        try:
            rate_limit = float(data.get('rate_limit', 0))
        except (ValueError, TypeError):
            rate_limit = 0
            
        try:
            word_count_threshold = int(data.get('word_count_threshold', 0))
        except (ValueError, TypeError):
            word_count_threshold = 0
            
        # Build config with safe parameter handling
        config = {
            'depth': depth,
            'max_pages': max_pages,
            'excluded_domains': data.get('excluded_domains', []),
            'include_only_domains': data.get('include_only_domains', []),
            'extract_images': data.get('extract_images', False),
            'css_selector': data.get('css_selector'),
            'target_elements': data.get('target_elements'),
            'follow_external_links': data.get('follow_external_links', False),
            'word_count_threshold': word_count_threshold,
            'excluded_tags': data.get('excluded_tags', []),
            'exclude_external_links': data.get('exclude_external_links', False),
            'exclude_social_media_links': data.get('exclude_social_media_links', False),
            'exclude_domains': data.get('exclude_domains', []),
            'exclude_external_images': data.get('exclude_external_images', False),
            'process_iframes': data.get('process_iframes', False),
            'page_interactions': data.get('page_interactions', []),
            'wait_for_selectors': data.get('wait_for_selectors', []),
            'extract_metadata': data.get('extract_metadata', False),
            'media_download': data.get('media_download', False),
            'media_types': data.get('media_types', ['image']),
            'user_agent': data.get('user_agent'),
            'timeout': timeout,
            'rate_limit': rate_limit,
            'extraction_schema': data.get('extraction_schema')
        }
        
        # Ensure target_elements is always a list if provided
        if 'target_elements' in data and data['target_elements'] is not None:
            if isinstance(data['target_elements'], str):
                config['target_elements'] = [data['target_elements']]
                
        synchronous = data.get('synchronous', False)
        callback_url = data.get('callback_url')

    if not url:
        return jsonify({"error": "URL parameter is missing"}), 400

    # Add timestamp to active crawls
    current_time = time.time()

    # For synchronous requests, run the crawl and return results immediately
    if synchronous:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(crawl_website(url, config))
            return jsonify(result)
        except Exception as e:
            logger.error(f"Synchronous crawl error: {str(e)}")
            return jsonify({"error": f"Crawl failed: {str(e)}"}), 500
        finally:
            loop.close()
    
    # For asynchronous requests, create a task and return task ID
    task_id = f"task_{uuid.uuid4()}"
    active_crawls[task_id] = {
        "url": url,
        "config": config,
        "status": "running",
        "callback_url": callback_url,
        "created_at": current_time
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

@app.route('/crawlllm', methods=['POST'])
def crawl_with_llm():
    """
    Endpoint to crawl a website and process the content with an LLM
    
    POST parameters (JSON):
    - url: Target URL to crawl
    - apikey: OpenAI API key
    - prompt: User prompt/instruction for the LLM
    - model: OpenAI model to use (default: "gpt-4o-mini")
    - extraction_type: "schema" or "block" (default: "block")
    - input_format: "markdown", "html", or "fit_markdown" (default: "markdown")
    - chunk_token_threshold: Maximum tokens per chunk (default: 1000)
    - apply_chunking: Whether to chunk content (default: True)
    - overlap_rate: Overlap ratio between chunks (default: 0.1)
    - synchronous: If true, wait for crawl to complete (default: false)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    # Extract required parameters
    url = data.get('url')
    api_key = data.get('apikey')
    prompt = data.get('prompt')
    
    if not url:
        return jsonify({"error": "URL parameter is missing"}), 400
    if not api_key:
        return jsonify({"error": "API key parameter is missing"}), 400
    if not prompt:
        return jsonify({"error": "Prompt parameter is missing"}), 400
    
    # Extract optional parameters with defaults
    model = data.get('model', 'gpt-4o-mini')
    extraction_type = data.get('extraction_type', 'block')
    input_format = data.get('input_format', 'markdown')
    synchronous = data.get('synchronous', False)
    
    try:
        chunk_token_threshold = int(data.get('chunk_token_threshold', 1000))
    except (ValueError, TypeError):
        chunk_token_threshold = 1000
        
    apply_chunking = data.get('apply_chunking', True)
    overlap_rate = float(data.get('overlap_rate', 0.1))
    
    # Create LLM extraction strategy
    llm_config = LLMConfig(
        provider=f"openai/{model}", 
        api_token=api_key
    )
    
    llm_strategy = LLMExtractionStrategy(
        llm_config=llm_config,
        extraction_type=extraction_type,
        instruction=prompt,
        chunk_token_threshold=chunk_token_threshold,
        overlap_rate=overlap_rate,
        apply_chunking=apply_chunking,
        input_format=input_format,
        extra_args={"temperature": 0.7}
    )
    
    # Create crawler config with LLM strategy
    crawl_config = CrawlerRunConfig(
        extraction_strategy=llm_strategy
    )
    
    # Define the async function to process the crawl
    async def run_crawl_with_llm():
        try:
            # Run the crawler with LLM extraction
            async with AsyncWebCrawler() as crawler:
                result = await crawler.arun(url=url, config=crawl_config)
                
                # Prepare response
                response_data = {
                    "url": url,
                    "success": result.success if hasattr(result, 'success') else False
                }
                
                # Add extracted content if available
                if hasattr(result, 'extracted_content') and result.extracted_content:
                    response_data["extracted_content"] = result.extracted_content
                    
                # Add error message if failed
                if hasattr(result, 'success') and not result.success and hasattr(result, 'error_message'):
                    response_data["error_message"] = result.error_message
                    
                # Add token usage information if available
                try:
                    usage_info = {}
                    if hasattr(llm_strategy, 'usages'):
                        usage_info["usages"] = llm_strategy.usages
                    if hasattr(llm_strategy, 'total_usage'):
                        usage_info["total_usage"] = llm_strategy.total_usage
                    
                    if usage_info:
                        response_data["usage"] = usage_info
                except Exception as e:
                    logger.warning(f"Failed to get usage info: {str(e)}")
                
                return response_data
                
        except Exception as e:
            error_message = str(e)
            logger.error(f"Error in crawlllm: {error_message}")
            return {
                "url": url,
                "success": False,
                "error_message": error_message
            }
    
    # For synchronous requests, run the crawl and return results immediately
    if synchronous:
        logger.info(f"Starting synchronous LLM crawl of {url}")
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_crawl_with_llm())
            return jsonify(result)
        except Exception as e:
            logger.error(f"Synchronous LLM crawl error: {str(e)}")
            return jsonify({"error": f"LLM crawl failed: {str(e)}"}), 500
        finally:
            loop.close()
    
    # For asynchronous requests, create a task and return task ID
    task_id = f"task_{uuid.uuid4()}"
    active_crawls[task_id] = {
        "url": url,
        "config": crawl_config,
        "status": "running",
        "created_at": time.time()
    }
    
    # Function to run the async function in a thread
    def thread_target():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(run_crawl_with_llm())
            
            # Update task info
            active_crawls[task_id]["status"] = "completed"
            active_crawls[task_id]["result"] = result
            active_crawls[task_id]["completed_at"] = time.time()
            
            # Store in cache for future retrieval
            crawl_results_cache[task_id] = {
                "result": result,
                "timestamp": time.time()
            }
        except Exception as e:
            error_message = str(e)
            logger.error(f"Error in async LLM crawl: {error_message}")
            active_crawls[task_id]["status"] = "failed"
            active_crawls[task_id]["error"] = error_message
        finally:
            loop.close()
    
    # Start the thread
    thread = threading.Thread(target=thread_target)
    thread.daemon = True
    thread.start()
    
    # Return task ID for status checking
    return jsonify({
        "task_id": task_id,
        "status": "running",
        "message": "Crawl with LLM started asynchronously"
    })

@app.route('/read_article', methods=['POST'])
def read_article():
    """
    Endpoint to convert text to an SEO-optimized article using Claude API
    
    POST parameters (JSON):
    - text: The text to convert (required)
    - apikey: Anthropic API key (required)
    - max_tokens: Maximum tokens for response (optional, default: 8192)
    - version: Anthropic API version (optional, default: 2023-06-01)
    - model: Claude model to use (optional, default: claude-3-5-haiku-20241022)
    - prompt: Custom prompt to use instead of the default SEO prompt (optional)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    # Extract required parameters
    text = data.get('text')
    api_key = data.get('apikey')
    
    if not text:
        return jsonify({"error": "Text parameter is missing"}), 400
    if not api_key:
        return jsonify({"error": "API key parameter is missing"}), 400
    
    # Extract optional parameters with defaults
    max_tokens = int(data.get('max_tokens', 8192))
    version = data.get('version', '2023-06-01')
    model = data.get('model', 'claude-3-5-haiku-20241022')
    custom_prompt = data.get('prompt')
    
    # Call the convert_to_article function with the custom prompt if provided
    result = convert_to_article(text, api_key, max_tokens, version, model, custom_prompt)
    
    # Return success or error response
    if result.get('success'):
        return jsonify(result)
    else:
        return jsonify(result), 400

@app.route('/read_all_articles', methods=['POST'])
def read_all_articles_endpoint():
    """
    Endpoint to process a query and SERP items by sending them to external APIs
    
    POST parameters (JSON):
    - query: The search query (required)
    - SERP: List of SERP result items with href, text, etc. (required)
    - max_site: Maximum number of sites to process (optional)
    
    Each SERP item should have:
    - base_domain: Domain of the result
    - href: URL of the result
    - text: Text description of the result
    - title: Title of the result (optional)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    # Extract required parameters
    query = data.get('query')
    serp_items = data.get('SERP')
    
    # Extract optional max_site parameter
    max_site = data.get('max_site')
    if max_site is not None:
        try:
            max_site = int(max_site)
        except (ValueError, TypeError):
            return jsonify({"error": "max_site must be an integer"}), 400
    
    if not query:
        return jsonify({"error": "Query parameter is missing"}), 400
    if not serp_items or not isinstance(serp_items, list):
        return jsonify({"error": "SERP parameter must be a non-empty list"}), 400
    
    # Call the read_all_articles function with max_site parameter
    from crawl_api_backups.convert_to_article import read_all_articles
    result = read_all_articles(query, serp_items, max_site)
    
    # Return success or error response
    if result.get('success'):
        return jsonify(result)
    else:
        return jsonify(result), 400

@app.route('/convertj2s', methods=['POST'])
def convert_json_to_string():
    """
    Endpoint to convert JSON data to an organized HTML string without newlines
    
    POST parameters (JSON):
    - convert: The JSON data to convert to an HTML string
    - rewrite: Optional boolean to rewrite the content using AI (default: False)
    - model: OpenAI model to use for rewriting (required if rewrite=True)
    - api_key: OpenAI API key for rewriting (required if rewrite=True)
    - max_input: Maximum input length for rewriting (default: 10000)
    
    Returns:
    - converted: The HTML formatted version of the input data
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    convert_data = data.get('convert')
    if convert_data is None:
        return jsonify({"error": "convert parameter is missing"}), 400
        
    # Check if rewrite is requested
    rewrite = data.get('rewrite', False)
    
    # Convert the data to an organized HTML string without any quotes
    try:
        # Check if the data is a list of sections
        if isinstance(convert_data, list):
            html_parts = []
            
            for section in convert_data:
                # Extract section data
                index = section.get('index', '')
                tags = section.get('tags', [])
                content = section.get('content', [])
                has_error = section.get('error', False)
                
                # Format tags as a comma-separated list
                tags_str = ', '.join(tags) if tags else ''
                
                # Add section header with index and tags
                if tags_str:
                    html_parts.append("<h2>Section " + str(index) + ": " + tags_str + "</h2>")
                else:
                    html_parts.append("<h2>Section " + str(index) + "</h2>")
                
                # Add content paragraphs
                for paragraph in content:
                    if paragraph:
                        # Replace any quotes in the content to avoid them in the output
                        clean_paragraph = str(paragraph).replace('"', '').replace("'", '')
                        html_parts.append("<p>" + clean_paragraph + "</p>")
                
                # Add error indicator if present
                if has_error:
                    html_parts.append("<p><strong>Note:</strong> This section contains errors.</p>")
                
                # Add separator between sections
                html_parts.append("<hr>")
            
            # Join all parts without newlines
            converted_string = "".join(html_parts)
        else:
            # Convert to string without quotes for non-list data
            try:
                # Try to convert to string representation without quotes
                converted_string = "<pre>" + str(convert_data).replace('"', '').replace("'", '') + "</pre>"
            except:
                # Fallback if that fails
                converted_string = "<pre>Unable to convert data</pre>"
        
        # If rewrite is requested, send the converted content to OpenAI for rewriting
        if rewrite:
            # Check for required parameters
            model = data.get('model')
            api_key = data.get('api_key')
            max_input = int(data.get('max_input', 10000))
            
            if not model:
                return jsonify({"error": "model parameter is required when rewrite=True"}), 400
            if not api_key:
                return jsonify({"error": "api_key parameter is required when rewrite=True"}), 400
            
            # Limit the input length
            if len(converted_string) > max_input:
                logger.info(f"Truncating content from {len(converted_string)} to {max_input} characters for rewriting")
                converted_string = converted_string[:max_input]
            
            # Prepare the prompt for rewriting
            prompt = f"Rewrite the following article as a engaging neutral writer and focus on facts: {converted_string}"
            
            # Call OpenAI API
            try:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                
                payload = {
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": "You are a professional content writer. Rewrite the provided content to be engaging, neutral, and factual."
                        },
                        {
                            "role": "user",
                            "content": prompt
                        }
                    ]
                }
                endpoint = "https://api.openai.com/v1/chat/completions"
                
                response = requests.post(
                    endpoint,
                    headers=headers,
                    json=payload
                )
                
                if response.status_code == 200:
                    result = response.json()
                    
                    # Extract the content from the response
                    rewritten_content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                    
                    # Return both the original and rewritten content
                    return jsonify({
                        "converted": converted_string,
                        "rewritten": rewritten_content,
                        "usage": result.get("usage", {})
                    })
                else:
                    # Return error from OpenAI with the original conversion
                    error_data = response.json() if response.content else {"message": "Unknown error"}
                    return jsonify({
                        "converted": converted_string,
                        "error": f"OpenAI API error: {response.status_code}",
                        "details": error_data
                    }), 200  # Still return 200 to provide the original conversion
            
            except Exception as e:
                logger.error(f"Error calling OpenAI API for rewriting: {str(e)}")
                return jsonify({
                    "converted": converted_string,
                    "error": f"Failed to rewrite content: {str(e)}"
                }), 200  # Still return 200 to provide the original conversion
        
        # If no rewrite requested, just return the converted string
        return jsonify({"converted": converted_string})
    except Exception as e:
        logger.error(f"Error converting data to HTML: {str(e)}")
        return jsonify({"error": f"Failed to convert data: {str(e)}"}), 500

@app.route('/convert_mass', methods=['POST'])
def convert_mass():
    """
    Endpoint to combine readed_format and url_source from multiple articles into a single string
    
    POST parameters (JSON):
    - articles: List of article objects, each containing readed_format and url_source
    - apikey: Claude API key (optional, only needed if you want to process the combined text)
    - process: Boolean flag to indicate if the combined text should be processed with Claude (default: false)
    - model: Claude model to use for processing (default: claude-3-5-haiku-20241022)
    - max_tokens: Maximum tokens for Claude response (default: 8192)
    - prompt: Custom prompt to use for processing with Claude (default: "Rewrite the content as an SEO expert")
    
    Returns:
    - combined_text: The combined text from all articles
    - processed_text: The processed text (if process=true and apikey provided)
    """
    data = request.get_json()
    if not data:
        return jsonify({"error": "Invalid JSON data"}), 400
        
    articles = data.get('articles')
    if not articles or not isinstance(articles, list):
        return jsonify({"error": "Articles parameter must be a non-empty list"}), 400
    
    # Combine readed_format and url_source from each article
    combined_parts = []
    for article in articles:
        readed_format = article.get('readed_format', '')
        url_source = article.get('url_source', '')
        
        if readed_format:
            combined_parts.append(readed_format)
        if url_source:
            combined_parts.append(url_source)
        combined_parts.append('--')  # Add separator between articles
    
    # Remove the last separator if it exists
    if combined_parts and combined_parts[-1] == '--':
        combined_parts.pop()
        
    # Join all parts with newlines
    combined_text = '\n'.join(combined_parts)
    
    # Check if processing with Claude is requested
    process = data.get('process', False)
    if process:
        api_key = data.get('apikey')
        if not api_key:
            return jsonify({
                "combined_text": combined_text,
                "warning": "No API key provided for processing"
            })
            
        # Process the combined text with Claude
        model = data.get('model', 'claude-3-5-haiku-20241022')
        max_tokens = int(data.get('max_tokens', 8192))
        
        # Get custom prompt if provided, otherwise use default
        custom_prompt = data.get('prompt')
        
        # Call convert_to_article with the custom prompt if provided
        result = convert_to_article(combined_text, api_key, max_tokens, "2023-06-01", model, custom_prompt)
        
        if result.get('success'):
            response_data = {
                "combined_text": combined_text,
                "processed_text": result.get('article'),
                "usage": result.get('usage')
            }
            
            # Add the prompt used to the response if a custom prompt was provided
            if custom_prompt:
                response_data["prompt_used"] = custom_prompt
                
            return jsonify(response_data)
        else:
            return jsonify({
                "combined_text": combined_text,
                "error": result.get('error'),
                "details": result.get('details', {})
            })
            
            if result.get('success'):
                return jsonify({
                    "combined_text": combined_text,
                    "processed_text": result.get('article'),
                    "usage": result.get('usage')
                })
            else:
                return jsonify({
                    "combined_text": combined_text,
                    "error": result.get('error'),
                    "details": result.get('details', {})
                })
    
    # Return just the combined text if no processing requested
    return jsonify({"combined_text": combined_text})

@app.route('/ai/chat', methods=['POST'])
def ai_chat():
    """
    Endpoint to interact with OpenAI's API
    
    POST parameters (JSON):
    - prompt: The user's prompt to send to the AI
    - model: The OpenAI model to use (e.g., "gpt-3.5-turbo", "gpt-4", "text-davinci-003")
    - api_key: OpenAI API key for authentication
    - system_message: Optional system message (default: "You are a helpful assistant.")
    - max_input: Optional integer to limit the length of the prompt (cuts off excess text)
    - is_stringified: Optional boolean indicating if the prompt is a stringified JSON (default: false)
    """
    data = request.get_json()
    if not data:
        # Log what was actually in the request when JSON parsing fails
        logger.error(f"Invalid JSON data received. Request content type: {request.content_type}")
        logger.error(f"Raw request data: {request.data[:1000] if request.data else 'Empty'}")
        
        # Prepare error response with request details
        error_response = {
            "error": "Invalid JSON data. Please ensure the request has Content-Type: application/json and contains valid JSON.",
            "request_details": {
                "content_type": request.content_type,
                "raw_data": request.data.decode('utf-8', errors='replace')[:1000] if request.data else 'Empty',
                "method": request.method,
                "headers": dict(request.headers)
            }
        }
        
        # Check if it's form data instead of JSON
        if request.form:
            logger.error(f"Form data detected: {dict(request.form)}")
            error_response["request_details"]["form_data"] = dict(request.form)
            error_response["error"] = "Invalid request format. Expected JSON but received form data."
            
        return jsonify(error_response), 400
        
    prompt = data.get('prompt')
    model = data.get('model', 'gpt-3.5-turbo')
    api_key = data.get('api_key')
    system_message = data.get('system_message', 'You are a helpful assistant.')
    max_input = data.get('max_input')
    is_stringified = data.get('is_stringified', False)
    
    if not prompt:
        return jsonify({"error": "Prompt parameter is missing"}), 400
    if not api_key:
        return jsonify({"error": "API key parameter is missing"}), 400
    
    # Handle stringified JSON prompt
    if is_stringified:
        try:
            # Parse the stringified JSON
            prompt = json.loads(prompt)
            logger.info("Successfully parsed stringified prompt")
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse stringified prompt: {str(e)}")
            return jsonify({"error": f"Invalid stringified prompt: {str(e)}"}), 400
        
    # Limit prompt length if max_input is specified
    if max_input is not None:
        try:
            max_input = int(max_input)
            if max_input > 0:
                if isinstance(prompt, str):
                    prompt = prompt[:max_input]
                    logger.info(f"Prompt truncated to {max_input} characters")
                else:
                    # For non-string prompts (like parsed JSON), convert to string first
                    prompt_str = json.dumps(prompt)
                    if len(prompt_str) > max_input:
                        logger.info(f"Complex prompt exceeds max_input ({len(prompt_str)} > {max_input})")
        except (ValueError, TypeError):
            logger.warning(f"Invalid max_input value: {max_input}, using full prompt")
    
    try:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        # Check if model is a chat model or a completion model
        is_chat_model = model.startswith(('gpt-3.5', 'gpt-4'))
        
        # Prepare the user message content based on prompt type
        if isinstance(prompt, dict) or isinstance(prompt, list):
            # For structured data, convert to formatted string
            user_content = json.dumps(prompt, indent=2)
            logger.info(f"Using structured prompt data (converted to JSON string)")
        else:
            # For string prompts, use as is
            user_content = prompt
        
        if is_chat_model:
            # Use chat completions API for chat models
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": user_content
                    }
                ]
            }
            
            endpoint = "https://api.openai.com/v1/chat/completions"
        else:
            # Use completions API for non-chat models with assistant role
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "assistant",
                        "content": system_message
                    },
                    {
                        "role": "user",
                        "content": user_content
                    }
                ]
            }
            
            endpoint = "https://api.openai.com/v1/chat/completions"
        
        # Make the request to OpenAI API
        response = requests.post(
            endpoint,
            headers=headers,
            json=payload
        )
        
        # Check if the request was successful
        if response.status_code == 200:
            result = response.json()
            return jsonify(result)
        else:
            # Return the error from OpenAI
            error_data = response.json() if response.content else {"message": "Unknown error"}
            return jsonify({
                "error": f"OpenAI API error: {response.status_code}",
                "details": error_data
            }), response.status_code
            
    except Exception as e:
        logger.error(f"Error calling OpenAI API: {str(e)}")
        return jsonify({"error": f"Failed to call OpenAI API: {str(e)}"}), 500

if __name__ == '__main__':
    app.start_time = time.time()
    app.run(host='0.0.0.0', port=5000)

