import {
    Page, Layout, Card, Text, Button, Box, InlineStack,
    BlockStack, Scrollable, Divider, Banner, Spinner, TextField
} from "@shopify/polaris";
import {
    type LoaderFunctionArgs,
    type ActionFunctionArgs,
    useLoaderData,
    useNavigate,
    useRevalidator,
    useFetcher,
} from "react-router";
import { authenticate } from "../shopify.server";
import { getJobsForShop, upsertJob, deleteJob, startImportWorker } from "../jobs.server";
import type { ImportJob } from "../jobs.server";
import React, { useEffect, useCallback, useState } from "react";
import { VendorListItem } from "../components/VendorListItem";

interface Vendor {
    id: string;
    name: string;
    count: number;
    shopifyCount: number;
    remainingCount: number;
    /** From tenant_brand_discounts — prefilled for import / queue. */
    discountPct: number;
}

export const loader = async ({ request }: LoaderFunctionArgs) => {
    const { session } = await authenticate.admin(request);
    const shopDomain = session.shop;
    const apiUrl = process.env.API_BASE_URL || "http://127.0.0.1:8000";
    const apiKey = process.env.BACKEND_API_KEY || "";

    let vendors: Vendor[] = [];
    try {
        console.log(`[Vendors Loader] Fetching from: ${apiUrl}/api/v1/products/brands/list`);
        
        const response = await fetch(`${apiUrl}/api/v1/products/brands/list`, {
            method: "GET",
            headers: {
                "Content-Type": "application/json",
                "X-API-Key": apiKey,
                "X-Shopify-Shop-Domain": shopDomain,
            },
        });
        
        if (response.ok) {
            const json = await response.json();
            vendors = (json.data || []).map((b: any, index: number) => ({
                id: String(b.id || index),
                name: String(b.name || "Unknown Brand"),
                count: Number(b.count || 0),
                shopifyCount: Number(b.shopify_count || 0),
                remainingCount: Number(b.remaining_count || 0),
                discountPct: Math.min(100, Math.max(0, Number(b.discount_pct ?? 0) || 0)),
            }));
        } else {
            // Log the error to the terminal
            const errorText = await response.text();
            console.error(`[Vendors Loader] API Error ${response.status}:`, errorText);
        }
    } catch (error) {
        // Log connection issues
        console.error(`[Vendors Loader] Fetch failed:`, error);
    }

    const jobs = getJobsForShop(shopDomain);
    return { vendors, jobs };
};

export const action = async ({ request }: ActionFunctionArgs) => {
    const { session } = await authenticate.admin(request);
    const shopDomain = session.shop;
    const data = await request.json();
    const apiUrl = process.env.API_BASE_URL || "http://127.0.0.1:8000";

    if (data._action === "job_update") {
        const { vendorId, vendorName, totalCount, progress, status, discountPct } = data;
        const apiKey = process.env.BACKEND_API_KEY || "";
        const payload: {
            shop: string;
            vendorId: string;
            vendorName: string;
            totalCount: number;
            progress: number;
            status: string;
            discountPct?: number;
        } = {
            shop: shopDomain,
            vendorId,
            vendorName,
            totalCount,
            progress,
            status,
        };
        if (discountPct !== undefined && discountPct !== null && discountPct !== "") {
            const n = Number(discountPct);
            if (!Number.isNaN(n)) {
                payload.discountPct = Math.min(100, Math.max(0, n));
            }
        }
        upsertJob(payload);
        if (status === "running") {
            startImportWorker(shopDomain, vendorId, apiUrl, apiKey);
        }
        return { ok: true };
    }

    if (data._action === "job_delete") {
        deleteJob(shopDomain, data.vendorId);
        return { ok: true };
    }

    if (data._action === "discount_update") {
        const apiKey = process.env.BACKEND_API_KEY || "";
        try {
            const response = await fetch(`${apiUrl}/api/v1/products/discount`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-API-Key": apiKey,
                    "X-Shopify-Shop-Domain": shopDomain
                },
                body: JSON.stringify({
                    vendor: data.vendorName,
                    discount: Number(data.discountPct),
                    shop: shopDomain
                }),
            });
            const result = await response.json();
            if (!response.ok || result.status === "error") {
                return { status: "error", message: result.message || "Failed to apply discount" };
            }
            return { status: "success", message: result.message || "Discount applied successfully" };
        } catch (error) {
            return { status: "error", message: "Network error occurred" };
        }
    }

    return { status: "error", message: "Unknown action" };
};

export default function Vendors() {
    const { vendors, jobs } = useLoaderData<typeof loader>();
    const navigate = useNavigate();
    const { revalidate } = useRevalidator();
    const jobFetcher = useFetcher();

    // useState keeps the sections collapsed until opened on the client
    const [showNeed, setShowNeed] = useState(false);
    const [showImported, setShowImported] = useState(false);
    const [showNoPrice, setShowNoPrice] = useState(false);
    const [mounted, setMounted] = useState(false);
    const [queueDiscounts, setQueueDiscounts] = useState<Record<string, string>>({});

    useEffect(() => {
        setMounted(true);
    }, []);

    const handleStatusChange = useCallback((job: ImportJob, status: string, progress: number) => {
        const base: Record<string, string | number> = {
            _action: "job_update",
            vendorId: job.vendorId,
            vendorName: job.vendorName,
            totalCount: job.totalCount,
            progress,
            status,
        };
        if (typeof job.discountPct === "number" && !Number.isNaN(job.discountPct)) {
            base.discountPct = job.discountPct;
        }
        jobFetcher.submit(base, { method: "POST", encType: "application/json" });
    }, [jobFetcher]);

    const handleDelete = useCallback((vendorId: string) => {
        jobFetcher.submit(
            { _action: "job_delete", vendorId },
            { method: "POST", encType: "application/json" }
        );
    }, [jobFetcher]);

    const handleAddToQueue = useCallback((vendor: Vendor) => {
        const raw = queueDiscounts[vendor.id];
        let pct = vendor.discountPct;
        if (raw !== undefined && raw.trim() !== "") {
            const n = Number(raw);
            if (!Number.isNaN(n)) pct = n;
        }
        pct = Math.min(100, Math.max(0, pct));
        jobFetcher.submit(
            {
                _action: "job_update",
                vendorId: vendor.id,
                vendorName: vendor.name,
                totalCount: vendor.count,
                progress: 0,
                status: "idle",
                discountPct: pct,
            },
            { method: "POST", encType: "application/json" }
        );
    }, [jobFetcher, queueDiscounts]);

    const handleQueueDiscountCommit = useCallback((job: ImportJob, discountPct: number) => {
        jobFetcher.submit(
            {
                _action: "job_update",
                vendorId: job.vendorId,
                vendorName: job.vendorName,
                totalCount: job.totalCount,
                progress: job.progress,
                status: job.status,
                discountPct,
            },
            { method: "POST", encType: "application/json" }
        );
    }, [jobFetcher]);

    const handleApplyDiscount = useCallback((vendor: Vendor) => {
        let pct = vendor.discountPct;
        const raw = queueDiscounts[vendor.id];
        if (raw !== undefined && raw.trim() !== "") {
            const n = Number(raw);
            if (!Number.isNaN(n)) pct = n;
        }
        pct = Math.min(100, Math.max(0, pct));
        
        jobFetcher.submit(
            {
                _action: "discount_update",
                vendorName: vendor.name,
                discountPct: pct,
            },
            { method: "POST", encType: "application/json" }
        );
    }, [jobFetcher, queueDiscounts]);

    useEffect(() => {
        if (jobFetcher.state === "idle" && jobFetcher.data) {
            const data = jobFetcher.data as any;
            if (data.status === "success" && data.message) {
                shopify.toast.show(data.message);
            } else if (data.status === "error" && data.message) {
                shopify.toast.show(data.message, { isError: true });
            }
            const timer = setTimeout(() => revalidate(), 500);
            return () => clearTimeout(timer);
        }
    }, [jobFetcher.state, jobFetcher.data, revalidate]);

    const hasActiveImports = jobs.some((j: ImportJob) => j.status === "running");

    useEffect(() => {
        if (!hasActiveImports) return;
        const timer = setInterval(() => revalidate(), 5000);
        return () => clearInterval(timer);
    }, [hasActiveImports, revalidate]);

    const jobMap = new Map(jobs.map((j: ImportJob) => [j.vendorId, j]));
    const finishedJobs = jobs.filter((j: ImportJob) => j.status === "finished");
    const activeJobs = jobs.filter((j: ImportJob) => j.status !== "finished");
    const needImportVendors = vendors.filter((v: Vendor) => v.count > 0 && v.remainingCount > 0);
    const importedVendors = vendors.filter((v: Vendor) => v.count > 0 && v.remainingCount <= 0);
    const vendorsNoListPrice = vendors.filter((v: Vendor) => v.count === 0);

    return (
        <Page title="Vendor Import Dashboard" fullWidth>
            <Box paddingBlockEnd="400">
                <Button onClick={() => navigate("/app")}>Back to Home</Button>
            </Box>
            <Layout>
                <Layout.Section>
                    <Banner tone="info">
                        <Text as="p" variant="bodySm">
                            Counts and import include only products with a handle and at least one variant whose List price is a positive number. Items without a price are never imported.
                        </Text>
                    </Banner>
                </Layout.Section>
                <Layout.Section variant="oneHalf">
                    <Card padding="0">
                        <Box padding="400">
                            <Text variant="headingMd" as="h2">Available Vendors</Text>
                        </Box>
                        <Scrollable style={{ height: "70vh" }}>
                            <Box padding="400">
                                <BlockStack gap="400">

                                    {/* Available for Import */}
                                    <Card>
                                        <BlockStack gap="200">
                                            <InlineStack align="space-between" blockAlign="center">
                                                <Text as="h3" variant="headingSm">
                                                    Available for Import ({needImportVendors.length})
                                                </Text>
                                                {mounted && (
                                                    <Button variant="plain" onClick={() => setShowNeed(s => !s)}>
                                                        {showNeed ? "Hide" : "Show"}
                                                    </Button>
                                                )}
                                            </InlineStack>
                                            {mounted && showNeed && (
                                                <BlockStack gap="300">
                                                    {needImportVendors.map((vendor: Vendor) => {
                                                        const vendorJob = jobMap.get(vendor.id);
                                                        const isQueued = Boolean(vendorJob);
                                                        return (
                                                            <Card key={vendor.id} background="bg-surface-secondary">
                                                                <InlineStack align="space-between" blockAlign="center" wrap={false}>
                                                                    <Box>
                                                                        <Text variant="bodyMd" fontWeight="bold" as="h3">{vendor.name}</Text>
                                                                        <Text variant="bodySm" as="p" tone="subdued">
                                                                            With List price: {vendor.count} | Shopify: {vendor.shopifyCount}
                                                                        </Text>
                                                                        <Text variant="bodySm" as="p" tone="caution">
                                                                            Can import {vendor.remainingCount} more
                                                                        </Text>
                                                                    </Box>
                                                                    <InlineStack gap="300" blockAlign="center" wrap={false}>
                                                                        <div style={{ width: "160px" }}>
                                                                            <TextField
                                                                                label="Discount %"
                                                                                labelHidden
                                                                                type="number"
                                                                                prefix="Discount"
                                                                                suffix="%"
                                                                                min={0}
                                                                                max={100}
                                                                                value={queueDiscounts[vendor.id] ?? ""}
                                                                                placeholder={vendor.discountPct ? String(vendor.discountPct) : "0"}
                                                                                onChange={(val) => setQueueDiscounts((prev) => ({ ...prev, [vendor.id]: val }))}
                                                                                autoComplete="off"
                                                                            />
                                                                        </div>
                                                                        <Button onClick={() => handleAddToQueue(vendor)} disabled={isQueued}>
                                                                            {isQueued ? "Added" : "Add to Queue"}
                                                                        </Button>
                                                                    </InlineStack>
                                                                </InlineStack>
                                                            </Card>
                                                        );
                                                    })}
                                                    {needImportVendors.length === 0 && (
                                                        <Text as="p" tone="subdued">
                                                            Nothing priced left to import, or no brands with a positive List price.
                                                        </Text>
                                                    )}
                                                </BlockStack>
                                            )}
                                        </BlockStack>
                                    </Card>

                                    {/* Already Imported */}
                                    <Card>
                                        <BlockStack gap="200">
                                            <InlineStack align="space-between" blockAlign="center">
                                                <Text as="h3" variant="headingSm">
                                                    Already Imported ({importedVendors.length})
                                                </Text>
                                                {mounted && (
                                                    <Button variant="plain" onClick={() => setShowImported(s => !s)}>
                                                        {showImported ? "Hide" : "Show"}
                                                    </Button>
                                                )}
                                            </InlineStack>
                                            {mounted && showImported && (
                                                <BlockStack gap="300">
                                                    {importedVendors.map((vendor: Vendor) => (
                                                            <Card key={vendor.id} background="bg-surface-secondary">
                                                                <InlineStack align="space-between" blockAlign="center" wrap={false}>
                                                                    <Box>
                                                                        <Text variant="bodyMd" fontWeight="bold" as="h3">{vendor.name}</Text>
                                                                        <Text variant="bodySm" as="p" tone="subdued">
                                                                            With List price: {vendor.count} | Shopify: {vendor.shopifyCount}
                                                                        </Text>
                                                                        <Text variant="bodySm" as="p" tone="success">Fully imported</Text>
                                                                    </Box>
                                                                    <InlineStack gap="300" blockAlign="center" wrap={false}>
                                                                        <div style={{ width: "160px" }}>
                                                                            <TextField
                                                                                label="Discount %"
                                                                                labelHidden
                                                                                type="number"
                                                                                prefix="Discount"
                                                                                suffix="%"
                                                                                min={0}
                                                                                max={100}
                                                                                value={queueDiscounts[vendor.id] ?? String(vendor.discountPct ?? 0)}
                                                                                onChange={(val) => setQueueDiscounts((prev) => ({ ...prev, [vendor.id]: val }))}
                                                                                autoComplete="off"
                                                                            />
                                                                        </div>
                                                                        <Button 
                                                                            variant="primary" 
                                                                            onClick={() => handleApplyDiscount(vendor)}
                                                                            loading={jobFetcher.state !== "idle" && jobFetcher.formData?.get("_action") === "discount_update" && jobFetcher.formData?.get("vendorName") === vendor.name}
                                                                        >
                                                                            Apply
                                                                        </Button>
                                                                    </InlineStack>
                                                                </InlineStack>
                                                            </Card>
                                                    ))}
                                                    {importedVendors.length === 0 && (
                                                        <Text as="p" tone="subdued">No fully imported brands yet.</Text>
                                                    )}
                                                </BlockStack>
                                            )}
                                        </BlockStack>
                                    </Card>

                                    {/* Brands with no positive List price — not importable */}
                                    <Card>
                                        <BlockStack gap="200">
                                            <InlineStack align="space-between" blockAlign="center">
                                                <Text as="h3" variant="headingSm">
                                                    No List price (not importable) ({vendorsNoListPrice.length})
                                                </Text>
                                                {mounted && (
                                                    <Button variant="plain" onClick={() => setShowNoPrice((s) => !s)}>
                                                        {showNoPrice ? "Hide" : "Show"}
                                                    </Button>
                                                )}
                                            </InlineStack>
                                            {mounted && showNoPrice && (
                                                <BlockStack gap="300">
                                                    {vendorsNoListPrice.map((vendor: Vendor) => (
                                                        <Card key={vendor.id} background="bg-surface-secondary">
                                                            <Box>
                                                                <Text variant="bodyMd" fontWeight="bold" as="h3">{vendor.name}</Text>
                                                                <Text variant="bodySm" as="p" tone="subdued">
                                                                    No variants with a positive numeric List price for this vendor's handles.
                                                                </Text>
                                                            </Box>
                                                        </Card>
                                                    ))}
                                                    {vendorsNoListPrice.length === 0 && (
                                                        <Text as="p" tone="subdued">Every brand has at least one priced product.</Text>
                                                    )}
                                                </BlockStack>
                                            )}
                                        </BlockStack>
                                    </Card>

                                    {vendors.length === 0 && (
                                        <Text as="p" tone="subdued">No vendors found.</Text>
                                    )}
                                </BlockStack>
                            </Box>
                        </Scrollable>
                    </Card>
                </Layout.Section>

                <Layout.Section variant="oneHalf">
                    <Card padding="0">
                        <Box padding="400">
                            <InlineStack align="space-between" blockAlign="center">
                                <Text variant="headingMd" as="h2">Import Queue</Text>
                                {hasActiveImports && <Spinner size="small" />}
                            </InlineStack>
                        </Box>
                        <Scrollable style={{ height: "70vh" }}>
                            <Box padding="400">
                                <BlockStack gap="400">
                                    {finishedJobs.length > 0 && (
                                        <>
                                            <Banner tone="success">
                                                <Text as="p" variant="bodySm">
                                                    {finishedJobs.length} import{finishedJobs.length > 1 ? "s" : ""} completed
                                                </Text>
                                            </Banner>
                                            {finishedJobs.map((job: ImportJob) => (
                                                <VendorListItem
                                                    key={job.vendorId}
                                                    job={job}
                                                    onStatusChange={handleStatusChange}
                                                    onRemove={() => handleDelete(job.vendorId)}
                                                    onQueueDiscountCommit={(pct) => handleQueueDiscountCommit(job, pct)}
                                                />
                                            ))}
                                            {activeJobs.length > 0 && <Divider />}
                                        </>
                                    )}
                                    {activeJobs.map((job: ImportJob) => (
                                        <VendorListItem
                                            key={job.vendorId}
                                            job={job}
                                            onStatusChange={handleStatusChange}
                                            onRemove={() => handleDelete(job.vendorId)}
                                            onQueueDiscountCommit={(pct) => handleQueueDiscountCommit(job, pct)}
                                        />
                                    ))}
                                    {jobs.length === 0 && (
                                        <Box paddingBlockStart="400">
                                            <Text as="p" tone="subdued" alignment="center">
                                                Queue is empty. Select a vendor from the left.
                                            </Text>
                                        </Box>
                                    )}
                                </BlockStack>
                            </Box>
                        </Scrollable>
                    </Card>
                </Layout.Section>
            </Layout>
        </Page>
    );
}