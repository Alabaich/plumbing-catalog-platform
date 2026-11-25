import { Page, Layout, CalloutCard, BlockStack, Text } from "@shopify/polaris";
import { useNavigate } from "react-router";
import React from "react";

export default function Index() {
    const navigate = useNavigate();

    return (
        <Page title="Plumbing Catalog Dashboard">
            <Layout>
                <Layout.Section>
                    <CalloutCard
                        title="Vendor Import System"
                        illustration="https://cdn.shopify.com/s/assets/admin/checkout/settings-customizecart-705f57c725ac05be5a34ec20c05b94298cb8afd10aac7bd9c7ad02030f48cfa0.svg"
                        primaryAction={{
                            content: 'Open Vendors Dashboard',
                            onAction: () => navigate('/app/vendors'),
                        }}
                    >
                        <p>
                            Manage your plumbing product catalogs and sync them directly from your vendors into your Shopify store.
                        </p>
                    </CalloutCard>
                </Layout.Section>
                <Layout.Section>
                    <BlockStack gap="400">
                        <Text as="h2" variant="headingMd">
                            Quick Guide
                        </Text>
                        <Text as="p" variant="bodyMd">
                            1. Go to the Vendors Dashboard.<br />
                            2. Select a vendor with ready products.<br />
                            3. Click "Import to Shopify" to start the background sync.
                        </Text>
                    </BlockStack>
                </Layout.Section>
            </Layout>
        </Page>
    );
}