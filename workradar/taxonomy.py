"""Role families and search terms. One list drives: job-board queries, title matching, families in the app.

Matching is on the job TITLE, as a whole phrase (word boundaries, hyphens/spaces interchangeable).
No loose keywords: "architect" alone is not a term (building architects), nor "designer" alone (interiors, fashion).
A title belongs to the first family whose term it contains, so put specific families first.
"""
import re

FAMILIES = [
    ("AI & automation", [
        "AI engineer", "AI product manager", "AI consultant", "AI strategist", "AI lead", "head of AI", "AI transformation",
        "AI governance", "AI ethics", "AI adoption", "AI specialist", "AI solutions", "generative AI", "GenAI", "LLM",
        "prompt engineer", "conversational designer", "conversation designer", "conversational AI", "chatbot", "machine learning",
        "ML engineer", "MLOps", "applied scientist", "automation engineer", "automation lead", "intelligent automation", "RPA",
        "Power Automate", "UiPath", "Copilot", "AI",
    ]),
    ("Business analysis", [
        "business analyst", "technical business analyst", "business systems analyst", "business process analyst",
        "requirements analyst", "process analyst", "functional analyst", "systems analyst", "data business analyst",
        "business analysis", "product analyst",
    ]),
    ("Delivery & agile", [
        "delivery manager", "delivery lead", "head of delivery", "agile delivery", "scrum master", "agile coach", "agile lead",
        "project manager", "programme manager", "program manager", "portfolio manager", "PMO", "project coordinator",
        "project lead", "release manager", "Jira", "Atlassian", "implementation manager", "technical project manager",
        "digital project manager", "programme director", "project director",
    ]),
    ("Product", [
        "product owner", "product manager", "product lead", "head of product", "product director", "product operations",
        "product ops", "platform product", "technical product", "digital product",
    ]),
    ("Design, UX & research", [
        "service designer", "service design", "UX designer", "UI designer", "UX/UI", "UI/UX", "product designer",
        "interaction designer", "content designer", "UX writer", "UX researcher", "user researcher", "UX research",
        "design manager", "head of design", "design lead", "design director", "design operations", "DesignOps", "design ops",
        "research operations", "ResearchOps", "design system", "accessibility",
    ]),
    ("Architecture & consulting", [
        "solution architect", "solutions architect", "enterprise architect", "technical architect", "data architect",
        "integration architect", "cloud architect", "business architect", "security architect", "technical consultant",
        "solutions consultant", "solution consultant", "implementation consultant", "functional consultant",
    ]),
    ("Transformation & change", [
        "transformation manager", "transformation lead", "transformation director", "head of transformation",
        "digital transformation", "business transformation", "change manager", "change analyst", "change lead",
        "business change", "change management", "operating model", "process improvement", "continuous improvement",
        "business improvement", "operational excellence", "organisational design", "Lean Six Sigma", "Six Sigma",
    ]),
    ("Digital, content & CMS", [
        "CMS", "headless", "content management", "Contentful", "Sitecore", "AEM", "Adobe Experience Manager", "Drupal",
        "WordPress", "Umbraco", "Storyblok", "Optimizely", "digital producer", "web producer", "content manager",
        "content strategist", "content strategy", "digital content", "web manager", "digital manager", "ecommerce manager",
        "e-commerce manager", "Shopify", "digital lead",
    ]),
    ("MarTech & digital marketing", [
        "MarTech", "marketing technology", "marketing operations", "marketing automation", "CRM manager", "CRM consultant",
        "CRM lead", "Salesforce Marketing Cloud", "HubSpot", "Braze", "Marketo", "digital marketing", "performance marketing",
        "growth marketing", "SEO", "PPC", "paid media", "campaign manager", "marketing analyst", "customer data platform",
        "CDP", "Adobe Analytics", "Google Analytics", "personalisation", "personalization", "web analytics",
        "product marketing", "marketing manager", "head of marketing", "marketing lead", "marketing director", "paid social",
        "social media manager", "email marketing", "lifecycle marketing", "CRO",
    ]),
    ("Data & analytics", [
        "data analyst", "data scientist", "data engineer", "analytics engineer", "BI developer", "BI analyst",
        "business intelligence", "Power BI", "Tableau", "data product", "data governance", "data quality", "data migration",
        "insight analyst", "insights analyst", "analytics manager", "head of data", "data lead", "data strategy",
    ]),
    ("Platforms & business systems", [
        "Salesforce", "Dynamics 365", "Microsoft Dynamics", "ServiceNow", "SAP", "Workday", "Oracle Cloud", "NetSuite",
        "CRM", "ERP", "Pega", "Appian", "Power Platform", "SharePoint",
    ]),
]


def _rx(term):
    body = r"[\s\-/]*".join(re.escape(w) for w in re.split(r"[\s\-]+", term))
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


GENERIC = {"AI", "GenAI", "generative AI", "LLM", "machine learning", "Copilot", "automation engineer", "chatbot"}
TERM_RX = [(fam, term, _rx(term)) for fam, terms in FAMILIES for term in terms]
FAMILY_NAMES = [f for f, _ in FAMILIES]
ALL_TERMS = [t for _, terms in FAMILIES for t in terms]

# Job-board queries: whole terms, most specific first; tool names that are only meaningful in titles are left out.
SEARCH_TERMS = [t for t in ALL_TERMS if t not in ("AI", "CRM", "SAP", "Jira", "Atlassian", "accessibility", "headless", "LLM", "CDP")] + \
               ["AI consultant", "Jira administrator", "SAP consultant", "CRM consultant", "headless CMS"]
SEARCH_TERMS = list(dict.fromkeys(SEARCH_TERMS))


def classify(title):
    """(family, term) for a title, or (None, None). Longest matching term wins within the first family that matches."""
    if not title:
        return None, None
    for generic_pass in (False, True):  # broad terms ("AI", "machine learning") only decide when nothing more specific matches
        for fam, terms in FAMILIES:
            hits = [t for f, t, rx in TERM_RX if f == fam and (t in GENERIC) == generic_pass and rx.search(title)]
            if hits:
                return fam, max(hits, key=len)
    return None, None


def batch(terms, size, slot):
    """Rotate through terms: batch `slot` of `size` (wraps round)."""
    if not terms:
        return []
    n = max(1, (len(terms) + size - 1) // size)
    i = slot % n
    return terms[i * size:(i + 1) * size]
