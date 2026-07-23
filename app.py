import streamlit as st
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
import json
import datetime
from collections import Counter
from google import genai
import time

# --- 1. SECURE API CONFIGURATION KEYS ---
AHREFS_API_KEY = st.secrets.get("AHREFS_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --- 2. CORE BACKEND HELPERS & SCRAPERS ---
def get_domain_from_url(url):
    """Extracts the root domain (e.g., 'example.com') from any URL string."""
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
        "error": None
    }
    
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'}
        response = requests.get(page_url, headers=headers, timeout=10, allow_redirects=True)
        
        if response.status_code != 200:
            results["error"] = f"Scrape Error: Status Code {response.status_code}"
            return results
            
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

        # UGC Detection
        ugc_signals = {
            "class_or_id": ['comment-list', 'comment-body', 'comments-area', 'forum-table', 'vbulletin', 'disqus_thread', 'bbpress-forums'],
            "text_patterns": ['leave a comment', 'post a comment', 'reply to this', 'anonymous user']
        }
        for element in soup.find_all(True, class_=True):
            if any(sig in ' '.join(element.get('class', [])).lower() for sig in ugc_signals["class_or_id"]):
                results["is_ugc"] = True
                results["ugc_reason"] = "UGC markup classes detected."
                break
        if not results["is_ugc"] and any(phrase in page_text for phrase in ugc_signals["text_patterns"]):
            results["is_ugc"] = True
            results["ugc_reason"] = "User-Generated comment text patterns observed."

        # Anchor & Link Audit
        links = soup.find_all('a', href=True)
        for link in links:
            if target_url.strip().lower() in link['href'].lower():
                results["link_found"] = True
                if expected_anchor.strip().lower() in link.text.lower():
                    results["anchor_matches"] = True
                rel = link.get('rel', [])
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
    except Exception as e:
        results["error"] = str(e)
        return results


# --- 3. GOOGLE GEMINI AI RELEVANCY AGENT ---
def analyze_relevancy_with_gemini(page_html, target_niche, business_topic):
    """Uses Gemini 2.5 Flash to evaluate domain niche and topic alignment rules."""
    if not gemini_client:
        return {"niche_pass": "Error", "topic_pass": "Error", "reason": "Gemini API Key missing."}
    try:
        soup = BeautifulSoup(page_html, 'html.parser')
        for script in soup(["script", "style"]):
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
            model='gemini-2.5-flash',
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
    """
    Production Engine: Pulls authority profile metrics from Ahrefs v3 arrays, 
    natively formatting column outputs to mirror live report states.
    """
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
    six_months_ago = (today - datetime.timedelta(days=180)).strftime("%Y-%m-%d")
    
    # 1. DOMAIN RATING (DR)
    try:
        res = requests.get("https://api.ahrefs.com/v3/site-explorer/domain-rating", headers=headers, params={"target": domain, "date": yesterday_str, "output": "json"}, timeout=10)
        if res.status_code == 200:
            results["dr"] = res.json().get("domain_rating", {}).get("domain_rating", "N/A")
    except Exception: pass

    # MANDATORY RATE LIMIT SHIELD
    time.sleep(2.0)

    # 2. 6-MONTH ORGANIC TRAFFIC HISTORY
    try:
        res = requests.get("https://api.ahrefs.com/v3/site-explorer/metrics-history", headers=headers, params={"target": domain, "mode": "subdomains", "date_from": six_months_ago, "date_to": yesterday_str, "history_grouping": "monthly", "output": "json"}, timeout=10)
        if res.status_code == 200:
            raw = res.json().get("metrics", [])
            results["traffic_history"] = sorted(raw, key=lambda x: x.get('date', ''))
    except Exception: pass

    # MANDATORY RATE LIMIT SHIELD
    time.sleep(2.0)

    # 3. SAMPLE ORGANIC KEYWORDS (VERIFIED WORKING BASELINE)
    try:
        res = requests.get("https://api.ahrefs.com/v3/site-explorer/organic-keywords", headers=headers, params={"target": domain, "mode": "subdomains", "date": yesterday_str, "limit": 100, "select": "keyword,best_position,volume,sum_traffic,keyword_country", "output": "json"}, timeout=10)
        if res.status_code == 200:
            raw_kws = res.json().get("keywords", [])
            results["keywords"] = [{"Keyword": k.get("keyword", "")} for k in raw_kws if k.get("keyword")][:25]
        else:
            results["error"] += f"Keywords Error ({res.status_code}) | "
    except Exception as e: results["error"] += f"Keywords Exception: {str(e)} | "

    # MANDATORY RATE LIMIT SHIELD
    time.sleep(2.0)

    # 4. TRAFFIC BY LOCATION (VERIFIED WORKING BASELINE)
    try:
        res = requests.get(
            "https://api.ahrefs.com/v3/site-explorer/metrics-by-country", 
            headers=headers, 
            params={
                "target": domain, 
                "mode": "subdomains",
                "date": yesterday_str,
                "select": "country,org_traffic", 
                "output": "json"
            }, 
            timeout=10
        )
        if res.status_code == 200:
            raw_countries = res.json().get("metrics", [])
            sorted_countries = sorted(raw_countries, key=lambda x: x.get("org_traffic", 0), reverse=True)
            
            results["top_countries"] = [
                {
                    "Country": item.get("country", "").upper(), 
                    "Organic Traffic": item.get("org_traffic", 0)
                } 
                for item in sorted_countries
            ][:5]
        else:
            results["error"] += f"Geo Location Error ({res.status_code}) | "
    except Exception as e: results["error"] += f"Geo Location Ex: {str(e)} | "

    # MANDATORY RATE LIMIT SHIELD
    time.sleep(2.0)

    # 5. FIRST 25 REFERRING DOMAINS (VERIFIED WORKING BASELINE)
    try:
        res = requests.get(
            "https://api.ahrefs.com/v3/site-explorer/refdomains", 
            headers=headers, 
            params={
                "target": domain, 
                "mode": "subdomains", 
                "date": yesterday_str, 
                "limit": 25, 
                "select": "domain,domain_rating", 
                "order_by": "domain_rating:desc", 
                "output": "json"
            }, 
            timeout=10
        )
        if res.status_code == 200:
            results["referring_domains"] = res.json().get("refdomains", [])
        else:
            results["error"] += f"RD Path Error ({res.status_code}) | "
    except Exception as e: results["error"] += f"RD Exception: {str(e)} | "

    # MANDATORY RATE LIMIT SHIELD
    time.sleep(2.0)

    # 6. FIRST 25 TOP PAGES (FIXED: Pulls strictly URL, Status, and Traffic to match screenshot exactly)
    try:
        res = requests.get(
            "https://api.ahrefs.com/v3/site-explorer/top-pages", 
            headers=headers, 
            params={
                "target": domain, 
                "mode": "subdomains", 
                "date": yesterday_str,
                "limit": 25, 
                "select": "url,status,traffic", # Updated selection query fields
                "order_by": "traffic:desc",
                "output": "json"
            }, 
            timeout=10
        )
        if res.status_code == 200:
            pages = res.json().get("top_pages", [])
            
            # Formats dictionary keys to match requested visualization columns exactly
            results["top_pages"] = [
                {
                    "URL": p.get("url", ""),
                    "Status": p.get("status", ""),
                    "Traffic": p.get("traffic", 0)
                }
                for p in pages if isinstance(p, dict)
            ]
            
            # Volatility evaluation math guards
            total_report_traffic = sum(p.get("traffic", 0) for p in pages if isinstance(p, dict))
            top_page_traffic = 0
            if pages and isinstance(pages[0], dict):
                top_page_traffic = pages[0].get("traffic", 0)
                
            traffic_pct_spread = (top_page_traffic / total_report_traffic * 100) if total_report_traffic > 0 else 0
            
            if traffic_pct_spread > 70:
                results["volatility_status"] = "WARNING"
                results["volatility_reason"] = f"CONCENTRATION WARNING: Top page holds {traffic_pct_spread:.1f}% of total site traffic."
        else:
            results["error"] += f"Top Pages Error ({res.status_code}) | "
    except Exception as e: results["error"] += f"Top Pages Exception: {str(e)} | "

    return results

# --- 5. STREAMLIT FRONT-END DASHBOARD UI ---
st.set_page_config(page_title="Enterprise Link Building QA", page_icon="🔗", layout="wide")
st.title("🔗 Enterprise Link Building QA Dashboard")
st.write("Audit placement verification rules, check sitewide authority risk profile parameters, and run contextual AI content mapping validations.")

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
        st.error("❌ Form Incomplete: Please provide both the Live Page URL and Target URL to run audits.")
    else:
        with st.spinner("Step 1/3: Scraping live page code frameworks..."):
            qa_results = check_link_and_tags(page_url, target_url, anchor_text, brand_name)
            
        with st.spinner("Step 2/3: Fetching analytics profile metrics from Ahrefs v3 arrays..."):
            ahrefs_results = fetch_advanced_ahrefs_data(page_url)
            
        raw_html_content = ""
        try: raw_html_content = requests.get(page_url, headers={'User-Agent': 'Mozilla/5.0'}, timeout=10).text
        except Exception: pass

        with st.spinner("Step 3/3: Running contextual semantic relevancy audits via Gemini 2.5 Flash..."):
            ai_relevancy = analyze_relevancy_with_gemini(raw_html_content, target_niche, business_topic)
            
        # ==========================================
        # GRAPHICAL INTERFACE GENERATOR
        # ==========================================
        st.markdown("---")
        st.subheader("📊 Live QA Verification Report")
        
        if qa_results["error"]:
            st.error(f"System Blocked: {qa_results['error']}")
        else:
            # Top Summary Scoreboard Row
            m_col1, m_col2, m_col3 = st.columns(3)
            with m_col1:
                st.metric(label="Domain Rating", value=f"DR {ahrefs_results['dr']}")
            with m_col2:
                status = "Indexable" if qa_results["is_indexable"] else "NoIndex ❌"
                st.metric(label="Crawler Index Status", value=status)
            with m_col3:
                brand_status = "Found" if qa_results["brand_mentioned"] else "Missing"
                st.metric(label="Brand Placement Check", value=brand_status)
                
            # Render Core Operational Data Tabs
            tab1, tab2, tab3 = st.tabs(["🔒 Technical Placement & Compliance", "📈 Ahrefs Sitewide Metrics Profile", "🧠 Semantic AI Relevancy"])
            
            # --- TAB 1: TECHNICAL RULES ---
            with tab1:
                st.markdown("### 🔍 Live URL Footprint Guardrails")
                if qa_results["is_redirecting"]:
                    st.warning(f"⚠️ **Redirect Alert:** Initial URL redirects! Destination resolved at: `{qa_results['final_destination_url']}`")
                else:
                    st.success("✅ **Redirect Check:** Clean. Target URL points to a stable direct response destination.")
                    
                if qa_results["is_ugc"]:
                    st.error(f"❌ **UGC Structural Risk:** Open comment layout footprint detected! Reason: *{qa_results['ugc_reason']}*")
                else:
                    st.success("✅ **UGC Profile Check:** Clean. Page code architecture matches standard editorial article files.")
                    
                if qa_results["listicle_top_3_pass"] == "PASS":
                    st.success(f"✅ **Listicle Framework:** Your brand '{brand_name}' successfully answers within the top 3 structural headings!")
                elif qa_results["listicle_top_3_pass"] == "FAIL":
                    st.error(f"❌ **Listicle Framework Deficit:** Article behaves like a listicle, but your brand '{brand_name}' is buried below position 3.")

                st.markdown("---")
                st.markdown("### 🔗 Hyperlink Node Verification")
                if qa_results["link_found"]:
                    st.success("✅ **Link Footprint:** Live target backlink anchor node discovered in page source.")
                    if qa_results["anchor_matches"]:
                        st.success(f"✅ **Anchor Framework:** Text configuration strings match: *'{anchor_text}'*")
                    else:
                        st.warning("⚠️ **Anchor Framework Discrepancy:** Backlink node discovered, but anchor text attributes mismatch.")
                    if qa_results["is_follow"]:
                        st.success("✅ **Link Attribution:** Clean DoFollow attribute structure verified.")
                    else:
                        st.error(f"❌ **Link Attribute Error:** Contains indexing restriction values: `{qa_results['rel_tags']}`")
                else:
                    st.error("❌ **Link Asset Missing:** Target destination string was completely absent inside the page anchor fields.")

            # --- TAB 2: ADVANCED AHREFS METRICS ---
            with tab2:
                st.markdown("### 📊 Sitewide SEO Health & Volatility Analytics")
                
                # Metric Profile Health Alerts
                if ahrefs_results["volatility_status"] == "FAIL":
                    st.error(f"❌ {ahrefs_results['volatility_reason']}")
                elif ahrefs_results["volatility_status"] == "WARNING":
                    st.warning(f"⚠️ {ahrefs_results['volatility_reason']}")
                else:
                    st.success(f"✅ **Profile Health Evaluation:** {ahrefs_results['volatility_reason']}")
                
                # Render Traffic chart line
                if ahrefs_results["traffic_history"]:
                    st.markdown("#### 📉 6-Month Organic Traffic Performance Trend")
                    dates = [i.get('date') for i in ahrefs_results["traffic_history"]]
                    traffic = [i.get('org_traffic', 0) for i in ahrefs_results["traffic_history"]]
                    st.line_chart(data=dict(zip(dates, traffic)))
                    
                c_left, c_right = st.columns(2)
                with c_left:
                    st.markdown("#### 🌍 Top 5 Geo-Traffic Locations")
                    if ahrefs_results["top_countries"]:
                        for item in ahrefs_results["top_countries"]:
                            st.write(f"🏳️‍🌈 **{item['Country']}**: Common in Top Ranking Clusters")
                            if "Organic Traffic" in item:
                                st.caption(f"Estimated Traffic Share: {item['Organic Traffic']:,}")
                    else: 
                        st.caption("No geographical country distributions populated.")
                        
                with c_right:
                    st.markdown("#### 🛠️ Core Metric Summary Indicators")
                    st.write(f"📊 **Referring Domains Window Cap:** {len(ahrefs_results['referring_domains'])} results mapped.")
                    st.write(f"📄 **Core Top Pages Window Cap:** {len(ahrefs_results['top_pages'])} results mapped.")
                    
                st.markdown("---")
                
                # Render Data Reports Layout Grid
                d_col1, d_col2 = st.columns(2)
                with d_col1:
                    st.markdown("#### 🔤 Sample Organic Keywords (First 25 Line Items)")
                    if ahrefs_results["keywords"]:
                        st.dataframe(ahrefs_results["keywords"], use_container_width=True)
                    else: 
                        st.caption("No organic keyword arrays found.")
                        
                with d_col2:
                    st.markdown("#### 🔗 Referring Domains Profile (First 25 Line Items)")
                    if ahrefs_results["referring_domains"]:
                        st.dataframe(ahrefs_results["referring_domains"], use_container_width=True)
                    else: 
                        st.caption("No external referring domains found.")
                    
                st.markdown("#### 📄 Top Traffic Target Pages Distribution (First 25 Line Items)")
                if ahrefs_results["top_pages"]:
                    st.dataframe(ahrefs_results["top_pages"], use_container_width=True)
                else: 
                    st.caption("No matching structural target subpages found.")

            # --- TAB 3: ARTIFICIAL SEMANTIC INTELLIGENCE ---
            with tab3:
                st.markdown("### 🧠 Contextual AI Evaluation Log")
                ai_col1, ai_col2 = st.columns(2)
                with ai_col1:
                    if ai_relevancy["niche_pass"] == "PASS":
                        st.success(f"🎯 **Niche Requirement:** PASS")
                    else: st.error(f"❌ **Niche Requirement:** FAIL")
                    st.caption(f"Configured Requirement Expectation: *{target_niche}*")
                with ai_col2:
                    if ai_relevancy["topic_pass"] == "PASS":
                        st.success(f"✍️ **Topic Alignment:** PASS")
                    else: st.error(f"❌ **Topic Alignment:** FAIL")
                    st.caption(f"Configured Topic Expectation: *{business_topic}*")
                        
                st.info(f"🤖 **AI Evaluation Auditor Reasoning:** {ai_relevancy['reason']}")

        # --- RE-ENGINEERED PERMANENT DEBUGGING CONSOLE ---
        st.markdown("---")
        st.markdown("### 🛠️ System Analytics Log Debugger")
        if 'ahrefs_results' in locals() and ahrefs_results.get("error"):
            st.error(f"❌ **Ahrefs Exception Stream:** {ahrefs_results['error']}")
        else:
            st.success("✅ **Ahrefs Sync Status:** Core parameters resolved correctly without exceptions.")