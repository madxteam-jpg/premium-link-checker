import streamlit as st
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote
import json
import datetime
import time
import requests
from curl_cffi import requests as cffi_requests
import cloudscraper
from google import genai
import io

# ReportLab PDF Libraries
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

# --- 1. SECURE API CONFIGURATION KEYS ---
AHREFS_API_KEY = st.secrets.get("AHREFS_API_KEY", "")
GEMINI_API_KEY = st.secrets.get("GEMINI_API_KEY", "")
SCRAPERANT_KEY = st.secrets.get("SCRAPERANT_KEY", "")

# Initialize Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


# --- 2. CORE BACKEND HELPERS & SCRAPERS ---
def fetch_url_content(url):
    """
    Multi-stage scraper pipeline:
    1. ScrapingAnt via standard 'requests'
    2. Direct Chrome TLS impersonation via curl_cffi
    3. Cloudscraper engine
    4. Jina Reader mirror fallback
    """
    if SCRAPERANT_KEY:
        try:
            encoded_url = quote(url, safe='')
            api_endpoint = (
                f"https://api.scrapingant.com/v2/general"
                f"?x-api-key={SCRAPERANT_KEY}"
                f"&url={encoded_url}"
                f"&browser=true"
            )
            ant_response = requests.get(api_endpoint, timeout=60)
            
            if ant_response.status_code == 200:
                return ant_response
            elif ant_response.status_code in [401, 403]:
                st.error("❌ **ScrapingAnt API Error:** Invalid API Key or out of credits.")
        except requests.exceptions.Timeout:
            st.warning("⚠️ **ScrapingAnt Timed Out:** Falling back to secondary scrapers...")
        except Exception as e:
            st.warning(f"⚠️ ScrapingAnt Connection Error: {e}")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    try:
        response = cffi_requests.get(
            url,
            impersonate="chrome124",
            headers=headers,
            timeout=10,
            allow_redirects=True
        )
        if response.status_code == 200:
            return response
    except Exception:
        pass

    try:
        scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        cs_response = scraper.get(url, timeout=12)
        if cs_response.status_code == 200:
            return cs_response
    except Exception:
        pass

    try:
        jina_url = f"https://r.jina.ai/{url}"
        jina_res = requests.get(jina_url, timeout=15)
        if jina_res.status_code == 200:
            return jina_res
    except Exception:
        pass

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
        "html_content": "",
        "error": None
    }
    
    response = fetch_url_content(page_url)
    
    if not response:
        results["error"] = "Network Failure: Unable to connect to target domain across all scrapers."
        return results

    if response.status_code != 200:
        results["error"] = f"Scrape Error: Status Code {response.status_code}"
        return results

    results["html_content"] = response.text

    if hasattr(response, 'history') and len(response.history) > 0:
        results["is_redirecting"] = True
        results["final_destination_url"] = response.url

    soup = BeautifulSoup(response.text, 'html.parser')
    page_text = soup.get_text().lower()
    
    if brand_name and brand_name.lower() in page_text:
        results["brand_mentioned"] = True

    robots_meta = soup.find('meta', attrs={'name': 'robots'})
    if robots_meta and 'noindex' in robots_meta.get('content', '').lower():
        results["is_indexable"] = False

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
            model="gemini-3.6-flash",
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
    
    try:
        res = requests.get("https://api.ahrefs.com/v3/site-explorer/domain-rating", headers=headers, params={"target": domain, "date": yesterday_str, "output": "json"}, timeout=10)
        if res.status_code == 200:
            results["dr"] = res.json().get("domain_rating", {}).get("domain_rating", "N/A")
    except Exception:
        pass

    time.sleep(1.0)

    try:
        res = requests.get("https://api.ahrefs.com/v3/site-explorer/metrics-history", headers=headers, params={"target": domain, "mode": "subdomains", "date_from": six_months_ago, "date_to": yesterday_str, "history_grouping": "monthly", "output": "json"}, timeout=10)
        if res.status_code == 200:
            raw = res.json().get("metrics", [])
            results["traffic_history"] = sorted(raw, key=lambda x: x.get('date', ''))
    except Exception:
        pass

    return results


# --- 5. PDF REPORT GENERATOR ENGINE ---
def generate_pdf_report(page_url, target_url, brand_name, anchor_text, qa_results, ahrefs_results, ai_relevancy):
    """Generates a downloadable PDF report summarizing all audit data."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()

    # Custom Report Typography & Styles
    title_style = ParagraphStyle('ReportTitle', parent=styles['Heading1'], fontSize=20, leading=24, textColor=colors.HexColor("#1E293B"))
    subtitle_style = ParagraphStyle('ReportSubtitle', parent=styles['Normal'], fontSize=10, leading=14, textColor=colors.HexColor("#64748B"))
    heading_style = ParagraphStyle('SectionHeading', parent=styles['Heading2'], fontSize=13, leading=16, textColor=colors.HexColor("#0F172A"), spaceBefore=12, spaceAfter=6)
    body_style = ParagraphStyle('BodyTextCustom', parent=styles['Normal'], fontSize=9, leading=12, textColor=colors.HexColor("#334155"))
    badge_pass = ParagraphStyle('PassBadge', parent=body_style, textColor=colors.HexColor("#166534"), fontName="Helvetica-Bold")
    badge_fail = ParagraphStyle('FailBadge', parent=body_style, textColor=colors.HexColor("#991B1B"), fontName="Helvetica-Bold")

    elements = []

    # Document Header
    elements.append(Paragraph("<b>Enterprise Link Building QA Audit</b>", title_style))
    elements.append(Paragraph(f"Generated on: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} UTC", subtitle_style))
    elements.append(Spacer(1, 10))
    elements.append(HRFlowable(width="100%", thickness=1, color=colors.HexColor("#CBD5E1"), spaceAfter=15))

    # Section 1: Target Specs & High-Level Summary
    elements.append(Paragraph("1. Audit Input Specifications & High-Level Summary", heading_style))
    
    spec_data = [
        [Paragraph("<b>Live Page URL:</b>", body_style), Paragraph(page_url, body_style)],
        [Paragraph("<b>Target URL:</b>", body_style), Paragraph(target_url, body_style)],
        [Paragraph("<b>Brand Name:</b>", body_style), Paragraph(brand_name or "N/A", body_style)],
        [Paragraph("<b>Expected Anchor:</b>", body_style), Paragraph(anchor_text or "N/A", body_style)],
    ]
    spec_table = Table(spec_data, colWidths=[120, 420])
    spec_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(spec_table)
    elements.append(Spacer(1, 10))

    summary_metrics = [
        [Paragraph("<b>Domain Rating</b>", body_style), Paragraph("<b>Crawler Index Status</b>", body_style), Paragraph("<b>Brand Placement</b>", body_style)],
        [
            Paragraph(f"DR {ahrefs_results.get('dr', 'N/A')}", body_style),
            Paragraph("PASS (Indexable)" if qa_results.get("is_indexable") else "FAIL (NoIndex)", badge_pass if qa_results.get("is_indexable") else badge_fail),
            Paragraph("PASS (Found)" if qa_results.get("brand_mentioned") else "FAIL (Missing)", badge_pass if qa_results.get("brand_mentioned") else badge_fail)
        ]
    ]
    summary_table = Table(summary_metrics, colWidths=[180, 180, 180])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 15))

    # Section 2: Technical Placement & Compliance
    elements.append(Paragraph("2. Technical Placement & Compliance Audit", heading_style))
    
    tech_checks = [
        ["Check Item", "Status", "Details / Diagnostics"],
        [
            "Redirect Inspection",
            "WARNING" if qa_results.get("is_redirecting") else "PASS",
            f"Destination: {qa_results.get('final_destination_url')}"
        ],
        [
            "UGC Structural Check",
            "FAIL" if qa_results.get("is_ugc") else "PASS",
            qa_results.get("ugc_reason") or "Clean article layout."
        ],
        [
            "Backlink Node Check",
            "PASS" if qa_results.get("link_found") else "FAIL",
            "Target link source anchor discovered." if qa_results.get("link_found") else "Target link missing from HTML source."
        ],
        [
            "Anchor Text Alignment",
            "PASS" if qa_results.get("anchor_matches") else "FAIL",
            f"Expected: '{anchor_text}'"
        ],
        [
            "Link Follow Attribution",
            "PASS" if qa_results.get("is_follow") else "RESTRICTED",
            f"Rel attributes: {qa_results.get('rel_tags')}" if qa_results.get("rel_tags") else "DoFollow"
        ]
    ]
    
    tech_table_data = []
    for row in tech_checks:
        status_p = Paragraph(f"<b>{row[1]}</b>", badge_pass if row[1] == "PASS" else badge_fail) if row[1] in ["PASS", "FAIL", "WARNING", "RESTRICTED"] else Paragraph(f"<b>{row[1]}</b>", body_style)
        tech_table_data.append([
            Paragraph(row[0], body_style),
            status_p,
            Paragraph(row[2], body_style)
        ])

    tech_table = Table(tech_table_data, colWidths=[150, 90, 300])
    tech_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor("#F1F5F9")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#CBD5E1")),
        ('PADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(tech_table)
    elements.append(Spacer(1, 15))

    # Section 3: Semantic AI Relevancy Audit
    elements.append(Paragraph("3. Semantic AI Relevancy Audit (Gemini Evaluation)", heading_style))
    ai_data = [
        [
            Paragraph("<b>Niche Relevancy:</b>", body_style),
            Paragraph(ai_relevancy.get("niche_pass", "N/A"), badge_pass if ai_relevancy.get("niche_pass") == "PASS" else badge_fail)
        ],
        [
            Paragraph("<b>Topic Alignment:</b>", body_style),
            Paragraph(ai_relevancy.get("topic_pass", "N/A"), badge_pass if ai_relevancy.get("topic_pass") == "PASS" else badge_fail)
        ],
        [
            Paragraph("<b>AI Reason Log:</b>", body_style),
            Paragraph(ai_relevancy.get("reason", "N/A"), body_style)
        ]
    ]
    ai_table = Table(ai_data, colWidths=[120, 420])
    ai_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor("#F8FAFC")),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
        ('PADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(ai_table)

    # Build Document
    doc.build(elements)
    buffer.seek(0)
    return buffer


# --- 6. STREAMLIT FRONT-END DASHBOARD UI ---
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


# --- 7. UNIFIED FORM SUBMISSION LOOP ---
if submitted:
    if not page_url or not target_url:
        st.error("❌ Form Incomplete: Please provide both the Live Page URL and Target URL.")
    else:
        with st.spinner("Step 1/3: Scraping live page code frameworks..."):
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
            
            with tab1:
                st.markdown("### 🔍 Live URL Footprint Guardrails")
                if qa_results["is_redirecting"]:
                    st.warning(f"⚠️ **Redirect Alert:** Destination resolved at: `{qa_results['final_destination_url']}`")
                else:
                    st.success("✅ **Redirect Check:** Clean direct destination.")
                    
                if qa_results["is_ugc"]:
                    st.error(f"❌ **UGC Structural Risk:** Comment layout detected! Reason: *{qa_results['ugc_reason']}*")
                else:
                    st.success("✅ **UGC Profile Check:** Clean editorial article layout verified.")

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

            with tab2:
                st.markdown("### 📊 Sitewide Authority Metrics")
                if ahrefs_results["traffic_history"]:
                    dates = [i.get('date') for i in ahrefs_results["traffic_history"]]
                    traffic = [i.get('org_traffic', 0) for i in ahrefs_results["traffic_history"]]
                    st.line_chart(data=dict(zip(dates, traffic)))

            with tab3:
                st.markdown("### 🧠 Contextual AI Evaluation Log")
                st.info(f"🤖 **AI Auditor Reasoning:** {ai_relevancy['reason']}")

            # --- DOWNLOAD REPORT BUTTON ---
            st.markdown("---")
            st.subheader("📥 Export Complete Audit Report")
            
            pdf_buffer = generate_pdf_report(
                page_url, 
                target_url, 
                brand_name, 
                anchor_text, 
                qa_results, 
                ahrefs_results, 
                ai_relevancy
            )
            
            st.download_button(
                label="📄 Download Full QA Audit Report (PDF)",
                data=pdf_buffer,
                file_name=f"Backlink_QA_Report_{get_domain_from_url(page_url)}.pdf",
                mime="application/pdf",
                use_container_width=True
            )
