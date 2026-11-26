# Search terms

**225 title terms in 11 role families.** Source of truth: `workradar/taxonomy.py`.

How they are used:

- **Matching:** a job belongs to a family only if its *title* contains one of these terms as a whole phrase. Loose words are not used: `architect` alone (buildings) and `designer` alone (interiors, fashion) are not terms.
- **Broad terms** (AI, Copilot, GenAI, LLM, automation engineer, chatbot, generative AI, machine learning) count only when nothing more specific matches, so “AI Business Analyst” is Business analysis.
- **Exclusions** remove sales, recruiting, legal, construction, facilities and similar titles even if they contain a term.
- **Job-board queries:** 219 of the terms are sent to Reed (30 per 2-hourly run, full sweep about every 16 hours) and Jooble (22 per 6-hourly run, 88 of its 100 daily requests, full sweep about every 2.5 days).
- **In the app:** pick a job title, or tick whole families under “Or whole families”.

## AI & automation (32)

AI engineer, AI product manager, AI consultant, AI strategist, AI lead, head of AI, AI transformation, AI governance, AI ethics, AI adoption, AI specialist, AI solutions, generative AI, GenAI, LLM, prompt engineer, conversational designer, conversation designer, conversational AI, chatbot, machine learning, ML engineer, MLOps, applied scientist, automation engineer, automation lead, intelligent automation, RPA, Power Automate, UiPath, Copilot, AI

## Business analysis (11)

business analyst, technical business analyst, business systems analyst, business process analyst, requirements analyst, process analyst, functional analyst, systems analyst, data business analyst, business analysis, product analyst

## Delivery & agile (22)

delivery manager, delivery lead, head of delivery, agile delivery, scrum master, agile coach, agile lead, project manager, programme manager, program manager, portfolio manager, PMO, project coordinator, project lead, release manager, Jira, Atlassian, implementation manager, technical project manager, digital project manager, programme director, project director

## Product (10)

product owner, product manager, product lead, head of product, product director, product operations, product ops, platform product, technical product, digital product

## Design, UX & research (24)

service designer, service design, UX designer, UI designer, UX/UI, UI/UX, product designer, interaction designer, content designer, UX writer, UX researcher, user researcher, UX research, design manager, head of design, design lead, design director, design operations, DesignOps, design ops, research operations, ResearchOps, design system, accessibility

## Architecture & consulting (14)

solution architect, solutions architect, enterprise architect, technical architect, data architect, integration architect, cloud architect, business architect, security architect, technical consultant, solutions consultant, solution consultant, implementation consultant, functional consultant

## Transformation & change (19)

transformation manager, transformation lead, transformation director, head of transformation, digital transformation, business transformation, change manager, change analyst, change lead, business change, change management, operating model, process improvement, continuous improvement, business improvement, operational excellence, organisational design, Lean Six Sigma, Six Sigma

## Digital, content & CMS (24)

CMS, headless, content management, Contentful, Sitecore, AEM, Adobe Experience Manager, Drupal, WordPress, Umbraco, Storyblok, Optimizely, digital producer, web producer, content manager, content strategist, content strategy, digital content, web manager, digital manager, ecommerce manager, e-commerce manager, Shopify, digital lead

## MarTech & digital marketing (36)

MarTech, marketing technology, marketing operations, marketing automation, CRM manager, CRM consultant, CRM lead, Salesforce Marketing Cloud, HubSpot, Braze, Marketo, digital marketing, performance marketing, growth marketing, SEO, PPC, paid media, campaign manager, marketing analyst, customer data platform, CDP, Adobe Analytics, Google Analytics, personalisation, personalization, web analytics, product marketing, marketing manager, head of marketing, marketing lead, marketing director, paid social, social media manager, email marketing, lifecycle marketing, CRO

## Data & analytics (19)

data analyst, data scientist, data engineer, analytics engineer, BI developer, BI analyst, business intelligence, Power BI, Tableau, data product, data governance, data quality, data migration, insight analyst, insights analyst, analytics manager, head of data, data lead, data strategy

## Platforms & business systems (14)

Salesforce, Dynamics 365, Microsoft Dynamics, ServiceNow, SAP, Workday, Oracle Cloud, NetSuite, CRM, ERP, Pega, Appian, Power Platform, SharePoint
