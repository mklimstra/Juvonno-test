# APPS Dash Registration Profiles
* Demonstrates OAuth2 authentication
* Loading API end points for filter sources
* Filtering profiles
* Pagination
* Downloading of aggregate paged data

# Development with GitHub Codespaces

This repository is configured for GitHub Codespaces. To start developing:

1. Click the green "Code" button on GitHub
2. Select the "Codespaces" tab
3. Click "Create codespace on main" (or your desired branch)

The environment will automatically:
- Set up Python 3.11
- Install all dependencies from `requirements.txt`
- Forward port 8050 for the Dash application
- Configure VS Code with Python extensions

After the codespace starts, create your `.env` file with the required credentials and run the application.

# Requirements

Create a '.env' file with CLIENT_ID, CLIENT_SECRET, APP_URL and SITE_URL. These require 
an application be added to the Oauth system on apps. The app url is the location of the
deployed dash app. The site url is the install of CSI Apps. This is apps.csipacific.ca in
production, but maybe localhost or 127.0.0.1 in local development.

* Application Registration: [https://apps.csipacific.ca/o/applications/](https://apps.csipacific.ca/o/applications/)

In live deployment, the variables are set within the secrets mechanism of the deployment
platform. In posit connect cloud, the app settings has a variables section:

![image](https://github.com/user-attachments/assets/a0570505-b647-4f36-a81b-4410420d5088)