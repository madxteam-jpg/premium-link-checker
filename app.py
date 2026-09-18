import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote
import json
import datetime
import time
from curl_cffi import requests as cffi_requests
from google import genai

# --- 1. SECURE API CONFIGURATION KEYS ---
AHREFS_API_KEY = st.secrets.get("AHREFS_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SCRAPERANT_KEY = st.secrets.get("SCRAPERANT_KEY", "")

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --- 2. CORE BACKEND HELPERS & SCRAPERS ---
def fetch_url_content(url):
    """
    Fetches web content using direct TLS browser impersonation first.
    If Cloudflare blocks with 403/503, falls back to ScrapingAnt residential proxies.
    """
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }

    try:
        # 1. Direct Request Attempt (Fast)
        response = cffi_requests.get(
            url,
            impersonate="chrome124",
            headers=headers,
            timeout=10,
            allow_redirects=True
        )
        
        if response.status_code == 200:
            return response

        # 2. ScrapingAnt Fallback for Cloudflare 403 / 503 WAF blocks
        if response.status_code in [403, 503] and SCRAPERANT_KEY:
            encoded_url = quote(url, safe='')
            
            # Formulating API Call using Residential Proxies & Headless Browser
            api_endpoint = (
                f"https://api.scrapingant.com/v2/general"
                f"?x-api-key={SCRAPERANT_KEY}"
                f"&url={encoded_url}"
                f"&browser=true"
                f"&proxy_type=residential"
            )
            
            # Increased timeout to 45 seconds to accommodate JS Turnstile execution
            ant_response = cffi_requests.get(
                api_endpoint,
                timeout=45
            )
            
            if ant_response.status_code == 200:
                return ant_response

        return response

    except Exception as e:
        st.error(f"Network Connection Exception: {str(e)}")
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
    """Local Scraper: Audits redirects, UGC indicators, listicle position, and backlink tags."""
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
        "html_content": "",  # Save HTML to pass to Gemini
        "error": None
    }
    
    response = fetch_url_content(page_url)
    
    if not response:
        results["error"] = "Network Failure: Could not establish connection to target destination."
        return results

    if response.status_code != 200:
        results["error"] = f"Scrape Error: Status Code {response.status_code}"
        return results

    # Save HTML to prevent duplicate HTTP requests
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

    # Targeted UGC Container Detection
    ugc_containers = soup.find_all(['div', 'section', 'ul', 'ol'], class_=True)
    ugc_classes = {'comment-list', 'comment-body', 'comments-area', 'forum-table', 'vbulletin', 'disqus_thread', 'bbpress-forums'}
    ugc_text_patterns = ['leave a comment', 'post a comment', 'reply to this', 'anonymous user']
    
    for element in ugc_containers:
        element_classes = set(' '.join(element.get('class', [])).lower().split())
        if element_classes.intersection(ugc_classes):
            results["is_ugc"] = True
            results["ugc_reason"] = "UGC markup container detected."
            break
            
    if not results["is_ugc"] and any(phrase in page_text for phrase in ugc_text_patterns):
        results["is_ugc"] = True
        results["ugc_reason"] = "User-Generated comment text patterns observed."

    # Anchor & Link Audit
    target_clean = target_url.strip().lower()
    expected_anchor_clean = expected_anchor.strip().lower() if expected_anchor else ""

    links = soup.find_all('a', href=True)
    for link in links:
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
        return {"niche_pass": "Error", "topic_pass": "Error", "reason": "No HTML content was retrieved."}

    try:
        soup = BeautifulSoup(page_html, 'html.parser')
        for script in soup(["script", "style", "nav", "footer", "header"]):
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
        "keywords": [],
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
    six_months_ago = (today - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
    
    # 1. DOMAIN RATING (DR)
    try:
        res = cffi_requests.get("https://api.ahrefs.com/v3/site-explorer/domain-rating", headers=headers, params={"target": domain, "date": yesterday_str, "output": "json"}, timeout=10)
        if res.status_code == 200:
            results["dr"] = res.json().get("domain_rating", {}).get("domain_rating", "N/A")
    except Exception:
        pass

    time.sleep(1.0)

    # 2. 6-MONTH ORGANIC TRAFFIC HISTORY
    try:
        res = cffi_requests.get("https://api.ahrefs.com/v3/site-explorer/metrics-history", headers=headers, params={"target": domain, "mode": "subdomains", "date_from": six_months_ago, "date_to": yesterday_str, "history_grouping": "monthly", "output": "json"}, timeout=10)
        if res.status_code == 200:
            raw = res.json().get("metrics", [])
            results["traffic_history"] = sorted(raw, key=lambda x: x.get('date', ''))
    except Exception:
        pass

    time.sleep(1.0)

    # 3. SAMPLE ORGANIC KEYWORDS
    try:
        res = cffi_requests.get("https://api.ahrefs.com/v3/site-explorer/organic-keywords", headers=headers, params={"target": domain, "mode": "subdomains", "date": yesterday_str, "limit": 100, "select": "keyword,best_position,volume,sum_traffic,keyword_country", "output": "json"}, timeout=10)
        if res.status_code == 200:
            raw_kws = res.json().get("keywords", [])
            results["keywords"] = [{"Keyword": k.get("keyword", "")} for k in raw_kws if k.get("keyword")][:25]
        else:
            results["error"] += f"Keywords Error ({res.status_code}) | "
    except Exception as e:
        results["error"] += f"Keywords Exception: {str(e)} | "

    return results


# --- 5. STREAMLIT FRONT-END DASHBOARD UI ---
st.set_page_config(page_title="Enterprise Link Building QA", page_icon="🔗", layout="wide")
st.title("🔗 Enterprise Link Building QA Dashboard")
st.write("Audit placement verification rules, check sitewide authority risk profiles, and execute AI content mapping validations.")

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
        with st.spinner("Step 1/3: Scraping live page code frameworks via TLS/ScrapingAnt..."):
            qa_results = check_link_and_tags(page_url, target_url, anchor_text, brand_name)
            
        with st.spinner("Step 2/3: Fetching analytics metrics from Ahrefs v3..."):
            ahrefs_results = fetch_advanced_ahrefs_data(page_url)

        with st.spinner("Step 3/3: Running contextual semantic relevancy audits via Gemini Flash..."):
            ai_relevancy = analyze_relevancy_with_gemini(
                qa_results.get("html_content", ""), 
                target_niche, 
                business_topic
            )
            
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
                
            tab1, tab2, tab3 = st.tabs(["🔒 Technical Placement & Compliance", "📈 Ahrefs Sitewide Metrics Profile", "🧠 Semantic AI Relevancy"])
            
            # --- TAB 1: TECHNICAL RULES ---
            with tab1:
                st.markdown("### 🔍 Live URL Footprint Guardrails")
                if qa_results["is_redirecting"]:
                    st.warning(f"⚠️ **Redirect Alert:** Initial URL redirects! Destination resolved at: `{qa_results['final_destination_url']}`")
                else:
                    st.success("✅ **Redirect Check:** Clean direct response destination.")
                    
                if qa_results["is_ugc"]:
                    st.error(f"❌ **UGC Structural Risk:** Comment layout detected! Reason: *{qa_results['ugc_reason']}*")
                else:
                    st.success("✅ **UGC Profile Check:** Clean editorial article layout verified.")
                    
                if qa_results["listicle_top_3_pass"] == "PASS":
                    st.success(f"✅ **Listicle Framework:** Brand '{brand_name}' ranked within the top 3 structural headings!")
                elif qa_results["listicle_top_3_pass"] == "FAIL":
                    st.error(f"❌ **Listicle Framework Deficit:** Brand '{brand_name}' is positioned below top 3 headings.")

                st.markdown("---")
                st.markdown("### 🔗 Hyperlink Node Verification")
                if qa_results["link_found"]:
                    st.success("✅ **Link Footprint:** Target backlink anchor node discovered in page source.")
                    if qa_results["anchor_matches"]:
                        st.success(f"✅ **Anchor Framework:** Matches expected string *'{anchor_text}'*")
                    else:
                        st.warning("⚠️ **Anchor Discrepancy:** Backlink node found, but anchor text mismatches.")
                    if qa_results["is_follow"]:
                        st.success("✅ **Link Attribution:** DoFollow attribute verified.")
                    else:
                        st.error(f"❌ **Link Attribute Error:** Contains indexing restriction flags: `{qa_results['rel_tags']}`")
                else:
                    st.error("❌ **Link Asset Missing:** Target destination string was not found inside page anchor elements.")

            # --- TAB 2: AHREFS METRICS ---
            with tab2:
                st.markdown("### 📊 Sitewide Authority Metrics")
                if ahrefs_results["traffic_history"]:
                    st.markdown("#### 📉 6-Month Organic Traffic Performance Trend")
                    dates = [i.get('date') for i in ahrefs_results["traffic_history"]]
                    traffic = [i.get('org_traffic', 0) for i in ahrefs_results["traffic_history"]]
                    st.line_chart(data=dict(zip(dates, traffic)))
                    
                st.markdown("#### 🔤 Sample Organic Keywords")
                if ahrefs_results["keywords"]:
                    st.dataframe(ahrefs_results["keywords"], use_container_width=True)
                else: 
                    st.caption("No organic keyword array populated.")

            # --- TAB 3: SEMANTIC AI RELEVANCY ---
            with tab3:
                st.markdown("### 🧠 Contextual AI Evaluation Log")
                ai_col1, ai_col2 = st.columns(2)
                with ai_col1:
                    if ai_relevancy["niche_pass"] == "PASS":
                        st.success("🎯 **Niche Requirement:** PASS")
                    else: 
                        st.error("❌ **Niche Requirement:** FAIL")
                    st.caption(f"Requirement Target: *{target_niche}*")
                with ai_col2:
                    if ai_relevancy["topic_pass"] == "PASS":
                        st.success("✍️ **Topic Alignment:** PASS")
                    else: 
                        st.error("❌ **Topic Alignment:** FAIL")
                    st.caption(f"Topic Target: *{business_topic}*")
                        
                st.info(f"🤖 **AI Auditor Reasoning:** {ai_relevancy['reason']}")
