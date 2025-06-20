# APPS Dash Registration Profiles
* Demonstrates OAuth2 authentication
* Loading API end points for filter sources
* Filtering profiles
* Pagination
* Downloading of aggregate paged data

# Requirements

Create a '.env' file with CLIENT_ID, CLIENT_SECRET, APP_URL and SITE_URL. These require 
an application be added to the Oauth system on apps. The app url is the location of the
deployed dash app. The site url is the install of CSI Apps. This is apps.csipacific.ca in
production, but maybe localhost or 127.0.0.1 in local development.

* Application Registration: [https://apps.csipacific.ca/o/applications/](https://apps.csipacific.ca/o/applications/)

In live deployment, the variables are set within the secrets mechanism of the deployment
platform. In posit connect cloud, the app settings has a variables section:

![image](https://github.com/user-attachments/assets/a0570505-b647-4f36-a81b-4410420d5088)