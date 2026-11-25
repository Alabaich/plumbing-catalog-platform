import { Links, Meta, Outlet, Scripts, ScrollRestoration, useLoaderData } from "react-router";
import { AppProvider } from "@shopify/polaris";
import enTranslations from "@shopify/polaris/locales/en.json";
import polarisStyles from "@shopify/polaris/build/esm/styles.css?url";
import React from "react";

export const loader = () => {
    return { apiKey: process.env.SHOPIFY_API_KEY || "" };
};

export const links = () => [
    { rel: "stylesheet", href: polarisStyles }
];

export default function App() {
    const { apiKey } = useLoaderData<typeof loader>();

    return (
        <html lang="en">
            <head>
                <meta charSet="utf-8" />
                <meta name="viewport" content="width=device-width, initial-scale=1" />
                <meta name="shopify-api-key" content={apiKey} />
                <script src="https://cdn.shopify.com/shopifycloud/app-bridge.js"></script>
                <Meta />
                <Links />
            </head>
            <body>
                <AppProvider i18n={enTranslations}>
                    <Outlet />
                </AppProvider>
                <ScrollRestoration />
                <Scripts />
            </body>
        </html>
    );
}