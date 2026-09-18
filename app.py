import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import json
import datetime
import time
import cloudscraper  # Bypasses Cloudflare 403 blocks
from google import genai

# --- 1. SECURE API CONFIGURATION KEYS ---
AHREFS_API_KEY = st.secrets.get("AHREFS_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --- 2. CORE BACKEND HELPERS & SCRAPERS ---
def fetch_url_content(url):
    """Fetches web page content using cloudscraper to bypass 403 / Cloudflare filters."""
    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        response = scraper.get(url, timeout=15)
        return response
    except Exception as e:
        st.error(f"Scraper connection failed: {e}")
        return None


def get_domain_from_url(url):
    """Extracts the root domain from any URL string."""
    try:
        parsed_domain = urlparse(url).netloc
        if parsed_domain.startswith("www."):
            parsed_domain = parsed_domain[4:]
        return parsed_domain
    except Exception:
        return None


def check_link_and_tags(page_url, target_url, expected_anchor, brand_name):
    """Audits redirects, UGC indicators, listicle position, and backlink tags."""
    results = {
        "link_found": False,
        "anchor_matches": False,
        "is_follow": True,
        "rel_tags": [],
        "brand_mentioned": False,
        "is_indexable": True,
        "is_ugc": False,
        "ugc_reason": "",
        "is_redirecting": False,
        "final_destination_url": page_url,
        "listicle_top_3_pass": "N/A",
        "html_content": "",  # Store HTML to reuse in Gemini
        "error": None
    }
    
    response = fetch_url_content(page_url)
    if not response:
        results["error"] = "Network/Cloudflare Failure: Could not reach destination."
        return results

    if response.status_code != 200:
        results["error"] = f"Scrape Error: Status Code {response.status_code}"
        return results

    # Save HTML for Gemini reuse
    results["html_content"] = response.text

    if len(response.history) > 0:
        results["is_redirecting"] = True
        results["final_destination_url"] = response.url

    soup = BeautifulSoup(response.text, 'html.parser')
    page_text = soup.get_text().lower()
    
    # Brand Mentions
    if brand_name and brand_name.lower() in page_text:
        results["brand_mentioned"] = True

    # Indexability
    robots_meta = soup.find('meta', attrs={'name': 'robots'})
    if robots_meta and 'noindex' in robots_meta.get('content', '').lower():
        results["is_indexable"] = False

    # Targeted UGC Detection (Scans container elements instead of every node)
    ugc_containers = soup.find_all(['div', 'section', 'ul', 'ol'], class_=True)
    ugc_classes = {'comment-list', 'comment-body', 'comments-area', 'forum-table', 'vbulletin', 'disqus_thread', 'bbpress-forums'}
    
    for element in ugc_containers:
        element_classes = set(' '.join(element.get('class', [])).lower().split())
        if element_classes.intersection(ugc_classes):
            results["is_ugc"] = True
            results["ugc_reason"] = "UGC markup container detected."
            break

    # Anchor & Link Audit
    target_clean = target_url.strip().lower()
    expected_anchor_clean = expected_anchor.strip().lower()

    for link in soup.find_all('a', href=True):
        if target_clean in link['href'].lower():
            results["link_found"] = True
            if expected_anchor_clean and expected_anchor_clean in link.text.lower():
                results["anchor_matches"] = True
            
            rel = link.get('rel', [])
            if isinstance(rel, str):
                rel = rel.split()
            results["rel_tags"] = rel
            
            if any(tag in ['nofollow', 'sponsored', 'ugc'] for tag in rel):
                results["is_follow"] = False
            break 

    # Listicle Placement Check
    page_title = soup.title.text.lower() if soup.title else ""
    listicle_triggers = ['best', 'top', 'tools', 'ways', 'apps', 'platforms', 'services']
    is_listicle = any(char.isdigit() for char in page_title) and any(w in page_title for w in listicle_triggers)
    
    if is_listicle and brand_name:
        headings = [h.text.strip().lower() for h in soup.find_all(['h1', 'h2', 'h3'])][:3]
        brand_in_top_3 = any(brand_name.lower() in heading for heading in headings)
        results["listicle_top_3_pass"] = "PASS" if brand_in_top_3 else "FAIL"

    return results


# --- 3. GOOGLE GEMINI AI RELEVANCY AGENT ---
def analyze_relevancy_with_gemini(page_html, target_niche, business_topic):
    """Uses Gemini Flash to evaluate domain niche and topic alignment rules."""
    if not gemini_client:
        return {"niche_pass": "Error", "topic_pass": "Error", "reason": "Gemini API Key missing."}
    if not page_html:
        return {"niche_pass": "Error", "topic_pass": "Error", "reason": "No HTML content received."}

    try:
        soup = BeautifulSoup(page_html, 'html.parser')
        for script in soup(["script", "style", "nav", "footer"]):
            script.decompose()
            
        pure_text = soup.get_text(separator=' ')
        truncated_text = " ".join(pure_text.split()[:1500])
        
        prompt = f"""
        You are an expert SEO Quality Assurance Auditor. Analyze this content to determine relevance.
        Target Niche/Industry: {target_niche}
        Client Business Topic/Core Product: {business_topic}

        Content:
        \"\"\"{truncated_text}\"\"\"

        Evaluate:
        1. Niche Relevancy: Is this page contextually adjacent or relevant to '{target_niche}'?
        2. Topic Relevancy: Does this theme make semantic sense to mention '{business_topic}'?
        """
        response = gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config=genai.types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema={
                    "type": "OBJECT",
                    "properties": {
                        "niche_pass": {"type": "STRING", "enum": ["PASS", "FAIL"]},
                        "topic_pass": {"type": "STRING", "enum": ["PASS", "FAIL"]},
                        "reason": {"type": "STRING"}
                    },
                    "required": ["niche_pass", "topic_pass", "reason"]
                },
                temperature=0.1
            )
        )
        return json.loads(response.text)
    except Exception as e:
        return {"niche_pass": "Error", "topic_pass": "Error", "reason": f"Gemini Exception: {str(e)}"}


# --- 4. ADVANCED AHREFS SITEWIDE ENGINE ---
def fetch_advanced_ahrefs_data(target_url):
    domain = get_domain_from_url(target_url)
    results = {
        "dr": "N/A",
        "traffic_history": None,
        "top_countries": [],
        "keywords": [],
        "referring_domains": [],
        "top_pages": [],
        "volatility_status": "PASS",
        "volatility_reason": "Profile health looks stable.",
        "error": ""
    }
    
    if not domain:
        results["error"] = "Invalid target domain format."
        return results
    if not AHREFS_API_KEY:
        results["error"] = "Ahrefs API key configuration is missing."
        return results

    headers = {"Authorization": f"Bearer {AHREFS_API_KEY}", "Accept": "application/json"}
    today = datetime.date.today()
    yesterday_str = (today - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 1. DOMAIN RATING (DR)
    try:
        res = requests.get(
            "https://api.ahrefs.com/v3/site-explorer/domain-rating", 
            headers=headers, 
            params={"target": domain, "date": yesterday_str, "output": "json"}, 
            timeout=10
        )
        if res.status_code == 200:
            results["dr"] = res.json().get("domain_rating", {}).get("domain_rating", "N/A")
    except Exception as e:
        results["error"] += f"Ahrefs DR Error: {e} | "

    return results


# --- 5. STREAMLIT FRONT-END DASHBOARD UI ---
st.set_page_config(page_title="Enterprise Link Building QA", page_icon="🔗", layout="wide")
st.title("🔗 Enterprise Link Building QA Dashboard")

with st.form("qa_form"):
    st.subheader("📋 Input Specifications")
    col1, col2 = st.columns(2)
    with col1:
        page_url = st.text_input("Live Page URL (Where your link is placed)")
        target_url = st.text_input("Target URL (Your Client Landing Page)")
        brand_name = st.text_input("Customer Brand Name")
    with col2:
        anchor_text = st.text_input("Expected Anchor Text")
        target_niche = st.text_input("Target Niche / Industry Requirements")
        business_topic = st.text_input("Client Core Business Topic / Product")
        
    submitted = st.form_submit_button("Execute Full System QA Audit")


# --- 6. UNIFIED FORM SUBMISSION LOOP ---
if submitted:
    if not page_url or not target_url:
        st.error("❌ Form Incomplete: Please provide both the Live Page URL and Target URL.")
    else:
        with st.spinner("Step 1/3: Scraping live page & checking links..."):
            qa_results = check_link_and_tags(page_url, target_url, anchor_text, brand_name)
            
        with st.spinner("Step 2/3: Fetching Ahrefs authority metrics..."):
            ahrefs_results = fetch_advanced_ahrefs_data(page_url)

        with st.spinner("Step 3/3: Running contextual AI relevancy audit..."):
            # Reuses HTML fetched in Step 1 to avoid a 2nd request
            ai_relevancy = analyze_relevancy_with_gemini(
                qa_results.get("html_content", ""), 
                target_niche, 
                business_topic
            )
            
        # UI Rendering Logic
        st.markdown("---")
        st.subheader("📊 Live QA Verification Report")
        
        if qa_results["error"]:
            st.error(f"System Blocked: {qa_results['error']}")
        else:
            m_col1, m_col2, m_col3 = st.columns(3)
            with m_col1:
                st.metric(label="Domain Rating", value=f"DR {ahrefs_results['dr']}")
            with m_col2:
                status = "Indexable" if qa_results["is_indexable"] else "NoIndex ❌"
                st.metric(label="Crawler Index Status", value=status)
            with m_col3:
                brand_status = "Found" if qa_results["brand_mentioned"] else "Missing"
                st.metric(label="Brand Placement Check", value=brand_status)
