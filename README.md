# Autonomous Job Hunter

CLI that reads the base CV in `docs/`, writes tailored CVs and cover letters, then searches the sites in `sites.csv` and applies. It stops for a human when a form is ambiguous, a login wall or CAPTCHA appears, or the submission confidence is below the threshold.

The tool does not invent employers, dates, degrees, or skills. Generated text stays inside the facts already in `docs/`.

## Setup

Python 3.14:

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional `.env` in the project root (existing environment variables are left alone):

```bash
JOBHUNTER_CHROME_MCP_COMMAND="npx -y @playwright/mcp@latest"
JOBHUNTER_MCP_TIMEOUT=45
JOBHUNTER_LLM_API_KEY=
JOBHUNTER_LLM_BASE_URL=https://api.openai.com/v1
JOBHUNTER_LLM_MODEL=gpt-4o-mini
```

`OPENAI_API_KEY` is also read when `JOBHUNTER_LLM_API_KEY` is empty. With no key, CV wording stays heuristic and grounded in the source documents.

Search and apply need Chrome MCP. Document generation does not.

`--chrome-profile` chooses the browser. `0` is an incognito window with no saved logins. Any other number is one of your Chrome profiles:

```bash
python main.py --list-chrome-profiles
python main.py --chrome-profile 0
python main.py --chrome-profile 1
```

`0` opens a fresh incognito Chrome window. Any other number opens each site as a tab in the Chrome window already running that profile. That profile needs the [Playwright extension](https://chromewebstore.google.com/detail/playwright-extension/mmlmfjhmonkocbjadbfplnigmagldckm) installed once; Chrome will not let a second browser share the same open profile. The number is saved and offered again the next time you are asked. If you leave the flag off, the same numbered list is printed in the terminal.

## Folders


| Path                                     | Role                                                                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| `docs/`                                  | Source CV and cover letter (`master_cv.txt` is the primary parse source; PDF and DOCX are also read)                |
| `sites.csv`                              | Job boards, one site at a time. Columns: `site_name,url,location_filter_param`                                      |
| `generated_docs/profile.json`            | Income, location, keywords, `skip_list`, `extra_job_titles`                                                         |
| `generated_docs/{area}/cv.pdf`           | Domain CV                                                                                                           |
| `generated_docs/{area}/cover_letter.pdf` | Matching cover letter                                                                                               |
| `applications/{job_id}/`                 | Sent-job record: `job_details.json`, `cv_used.pdf`, `cover_letter_used.pdf`, `snapshot.png` (or `page_source.html`) |
| `logs/jobhunter.log`                     | Run log                                                                                                             |


A job is not sent again when its id or source URL is already in `applications/`.

## Commands

Show every flag:

```bash
python main.py --help
```

Build the profile and the domain PDFs, without opening a browser:

```bash
python main.py --through documents --noninteractive
```

Search listings, still without applying:

```bash
python main.py --through search
```

Open the window:

```bash
python main.py
```

Apply from the terminal. Auto mode submits jobs that clear the confidence bar:

```bash
python main.py --through apply
```

1. **Analyze CV + Generate profile.json + Generate docs** reads `docs/` and writes the profile and CV pairs.
2. **Revise** edits income, location, keywords, the skip list, and extra titles, then writes any CV pair that is missing.
3. **Open site** lists each site in `sites.csv`. Choose one, then a Chrome profile. Incognito is first. The site opens, and **Read page** scores the job you opened.

The score page shows likelihood and success rate from 0 to 100, the reason, and the suggested CV. **Open folder** reveals that CV and cover letter. **Go back** returns to Read page. **Record** saves the application and returns to Read page.

The terminal standby flow is still available:

```bash
python main.py --standby
```


| Key | Action                                                                                                   |
| --- | -------------------------------------------------------------------------------------------------------- |
| `1` | Score the open job from 0 to 100, with a reason and a skip or proceed score. Then `0` goes back, `1` creates a new CV and cover letter |
| `a` | Fill the form and upload that pair. You press Submit                                                     |
| `q` | Quit                                                                                                     |


Search and apply with the CVs already in `generated_docs/`, without reading `docs/` or rewriting PDFs:

```bash
python main.py --skip-documents
python main.py --skip-documents --mode semi
```

Semi mode asks before each job. Enter `-` or `skip` to pass, `+` or `go` to continue:

```bash
python main.py --mode semi
```

Fill the form and stop before submit:

```bash
python main.py --dry-run
```

Accept saved or example preferences and skip any job that needs a person:

```bash
python main.py --noninteractive
```

Raise or lower the submit bar (default `0.8`). Open at most 5 listings per site. Print the log on the terminal:

```bash
python main.py --confidence 0.9 --max-per-site 5 --verbose
```

Point at another docs folder or sites file:

```bash
python main.py --docs /path/to/docs --sites /path/to/sites.csv
```



### Extra job titles

One title, appended to `extra_job_titles` in `profile.json`. Writes only that pair:

```bash
python main.py --extra-job-title "Forward Deployed Engineer"
```

Output:

```text
generated_docs/forward_deployed_engineer/cv.pdf
generated_docs/forward_deployed_engineer/cover_letter.pdf
```

A title already in the list is not duplicated. The PDFs for that title are refreshed.

Every saved title, one CV and cover letter each:

```bash
python main.py --extra-job-title
```

This command does not search or apply.

## Stages

`--through` stops after the named stage. Default is `apply`.


| Stage       | What it does                                                                 |
| ----------- | ---------------------------------------------------------------------------- |
| `profile`   | Read `docs/` and write `generated_docs/profile.json`                         |
| `documents` | Also write the domain CV and cover letter PDFs                               |
| `search`    | Also open each site and collect listings                                     |
| `apply`     | Also open each job, fill the form, and submit when confidence is high enough |


Domain PDFs, in this order, only when the source documents support them:

1. AI Director and Architect
2. Technical Project Manager
3. Business Analyst and Forward Deployed Engineer
4. Software Engineer



## Profile

`generated_docs/profile.json` fields you edit by hand:

- `targeted_income`
- `targeted_location`
- `interested_keywords` — search queries
- `skip_list` — a job is skipped when its title, company, location, description, or URL contains any phrase (case-insensitive)
- `extra_job_titles` — titles used by `--extra-job-title`

The first interactive run asks for income, location, keywords, skip list, and extra titles. Later runs reuse the saved profile unless you decline. `--noninteractive` reuses the file when it exists.

## When the CLI stops

It prompts you for salary and other ambiguous answers, for CAPTCHA, 2FA, and login walls, and whenever submission confidence is below `--confidence`. Passwords are typed with a hidden prompt and are not stored in `job_details.json`.

On a failed browser step the menu is: retry, continue, or skip. Continue on a required step skips that job. `--noninteractive` skips the job instead of asking.

## Tests

```bash
.venv/bin/python -m unittest tests.test_pipeline -q
```

