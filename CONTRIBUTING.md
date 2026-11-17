# Contributing to Elite Dangerous AI Integration

Thank you for your interest in contributing! Here's how to set up your development environment.

## Prerequisites

- **Python 3.12**: Make sure you have Python 3.12 installed. You can download it from [python.org](https://www.python.org/downloads/).
- **Node.js**: For Angular and Electron development.

## Getting the Code

1. **Fork the repository**: Click the "Fork" button on the top right of the repository page to create your own copy.

2. **Clone your fork**: Use the following command to clone your fork to your local machine:

   ```bash
   git clone your-fork-url
   ```

3. **Navigate to the project directory**:

   ```bash
   cd elite-dangerous-ai-integration
   ```

4. **Add the original repository as a remote** (optional but recommended):

   ```bash
   git remote add upstream official-repo-url
   ```

5. **Fetch the latest changes** from the original repository:

   ```bash
   git fetch upstream
   ```

6. **Create a new branch** for your changes:

   ```bash
   git checkout -b your-feature-branch
   ```

## Backend Setup (Python)

### Updating the module catalogs

From the repository root, run `python tools/import_catalog.py --outfitting-only`
to refresh the separate [outfitting catalog](src/assets/outfitting_catalog.json)
and its [coverage report](src/assets/catalog_report.json) without changing the
[engineering blueprint catalog](src/assets/module_catalog.json). Omit the flag
to regenerate both catalogs (including EDCD/coriolis-data blueprints). The
outfitting update downloads the latest
[EDCD/FDevIDs outfitting.csv](https://raw.githubusercontent.com/EDCD/FDevIDs/master/outfitting.csv),
validates the CSV headers, required values and unique IDs, and records the
CSV's SHA-256 digest in the outfitting catalog's provenance. Check the generated
diff and run `python -m pytest -q test/tools/ test/lib/projections/test_guardian_unlocks.py`
before committing. Runtime uses only committed assets, not the network;
restart the application to pick up an updated catalog. FDevIDs notes its list
is incomplete (notably some PowerPlay modules).

1. **Install Python 3.12**:

   Ensure you have at least Python 3.12 installed and available in your PATH. You can check this by running:

   ```bash
   python --version
   ```

   If you see a version number starting with `3.12` or above, you're good to go.

2. **Create a Virtual Environment:**

   Navigate to the project root directory in your terminal and run:

   ```bash
   python -m venv .venv
   ```

   This creates a virtual environment named `.venv` in the project root.

3. **Activate the Virtual Environment:**

   - On **Linux/macOS**:
     ```bash
     source .venv/bin/activate
     ```
   - On **Windows (Command Prompt)**:
     ```bat
     .venv\Scripts\activate.bat
     ```
   - On **Windows (PowerShell)**:
     ```powershell
     . .\.venv312\Scripts\Activate.ps1
     ```
     This activates the existing Python 3.12 virtual environment in the
     current PowerShell session.
     You should see `(.venv)` at the beginning of your terminal prompt.

4. **Install Dependencies:**

   With the virtual environment activated, install the required Python packages:

   ```bash
   pip install -r requirements.txt
   ```

## Frontend Setup (UI)

2.  **Install Node.js Dependencies:**

    ```bash
    npm run install:ui
    npm run install:electron
    ```

    This installs the necessary packages for the UI and Electron development.

## Running the Application in Development Mode

1.  **Run the Electron App in Development mode:**

    ```bash
    npm start
    ```

    This will build and launch the application with hot-reloading for the frontend. The Python backend is reloaded when stopping the AI Assistant, so start and stop the assistant to see changes, or manually reload the page using right-click > Reload.
