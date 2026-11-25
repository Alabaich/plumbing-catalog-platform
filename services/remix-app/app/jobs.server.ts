import fs from "fs";
import path from "path";

const JOBS_FILE = path.join(process.cwd(), "data", "import_jobs.json");

export interface ImportJob {
    id: string;
    shop: string;
    vendorId: string;
    vendorName: string;
    totalCount: number;
    progress: number;
    status: string; // idle | running | paused | finished | error
    /** Saved to API as tenant_brand_discounts; each import chunk sends this value. */
    discountPct?: number;
    createdAt: string;
    updatedAt: string;
}

const activeWorkers = new Set<string>();

function sleep(ms: number) {
    return new Promise(resolve => setTimeout(resolve, ms));
}

function makeKey(shop: string, vendorId: string) {
    return `${shop}::${vendorId}`;
}

function ensureDir() {
    const dir = path.dirname(JOBS_FILE);
    if (!fs.existsSync(dir)) {
        fs.mkdirSync(dir, { recursive: true });
    }
}

function readAll(): ImportJob[] {
    try {
        ensureDir();
        if (!fs.existsSync(JOBS_FILE)) return [];
        const raw = fs.readFileSync(JOBS_FILE, "utf-8");
        return JSON.parse(raw) as ImportJob[];
    } catch {
        return [];
    }
}

function writeAll(jobs: ImportJob[]) {
    ensureDir();
    fs.writeFileSync(JOBS_FILE, JSON.stringify(jobs, null, 2), "utf-8");
}

function getJob(shop: string, vendorId: string): ImportJob | undefined {
    return readAll().find(j => j.shop === shop && j.vendorId === vendorId);
}


export function getJobsForShop(shop: string): ImportJob[] {
    return readAll()
        .filter(j => j.shop === shop)
        .sort((a, b) => new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime());
}

export function upsertJob(data: Omit<ImportJob, "id" | "createdAt" | "updatedAt">) {
    const all = readAll();
    const now = new Date().toISOString();
    const idx = all.findIndex(j => j.shop === data.shop && j.vendorId === data.vendorId);
    if (idx >= 0) {
        all[idx] = { ...all[idx], ...data, updatedAt: now };
    } else {
        all.push({
            id: `${data.shop}-${data.vendorId}`,
            ...data,
            createdAt: now,
            updatedAt: now,
        });
    }
    writeAll(all);
}

export function deleteJob(shop: string, vendorId: string) {
    writeAll(readAll().filter(j => !(j.shop === shop && j.vendorId === vendorId)));
}

export function startImportWorker(
    shop: string,
    vendorId: string,
    apiBaseUrl: string,
    apiKey?: string,
) {
    const key = makeKey(shop, vendorId);
    if (activeWorkers.has(key)) {
        console.log(`[import] Worker already running for ${key}`);
        return;
    }
    activeWorkers.add(key);
    console.log(`[import] Starting worker for ${key}`);

    const resolvedApiKey = apiKey || process.env.BACKEND_API_KEY || "";

    void (async () => {
        try {
            while (true) {
                const job = getJob(shop, vendorId);
                if (!job) {
                    console.log(`[import] Job ${key} not found, stopping`);
                    return;
                }

                if (job.status !== "running") {
                    console.log(`[import] Job ${key} status=${job.status}, stopping worker`);
                    return;
                }

                if (job.progress >= job.totalCount) {
                    upsertJob({ ...job, status: "finished", progress: job.totalCount });
                    console.log(`[import] Job ${key} finished`);
                    return;
                }

                const body: Record<string, unknown> = {
                    vendor: job.vendorName,
                    shop,
                    offset: job.progress,
                    limit: 1,
                };
                if (typeof job.discountPct === "number" && !Number.isNaN(job.discountPct)) {
                    body.discount_pct = job.discountPct;
                }

                const response = await fetch(`${apiBaseUrl}/api/v1/products/import`, {
                    method: "POST",
                    headers: {
                        "Content-Type": "application/json",
                        "X-API-Key": resolvedApiKey,
                    },
                    body: JSON.stringify(body),
                });

                if (!response.ok) {
                    console.error(`[import] Job ${key} HTTP ${response.status}`);
                    upsertJob({ ...job, status: "error" });
                    return;
                }

                const latest = getJob(shop, vendorId);
                if (!latest) return;

                const nextProgress = latest.progress + 1;
                const nextStatus =
                    latest.status !== "running"
                        ? latest.status
                        : nextProgress >= latest.totalCount
                            ? "finished"
                            : "running";

                upsertJob({ ...latest, progress: nextProgress, status: nextStatus });

                if (nextStatus !== "running") {
                    console.log(`[import] Job ${key} => ${nextStatus}`);
                    return;
                }

                // Respect Shopify API rate limits: ~2 req/sec max
                await sleep(500);
            }
        } catch (err) {
            console.error(`[import] Worker ${key} crashed:`, err);
            const job = getJob(shop, vendorId);
            if (job) upsertJob({ ...job, status: "error" });
        } finally {
            activeWorkers.delete(key);
            console.log(`[import] Worker ${key} exited`);
        }
    })();
}


function recoverRunningJobs() {
    const jobs = readAll().filter(j => j.status === "running");
    if (jobs.length === 0) return;

    const apiBaseUrl = process.env.API_BASE_URL || "http://127.0.0.1:8000";
    const apiKey = process.env.BACKEND_API_KEY || "";

    console.log(`[import] Recovering ${jobs.length} running job(s) after restart`);
    for (const job of jobs) {
        startImportWorker(job.shop, job.vendorId, apiBaseUrl, apiKey);
    }
}

recoverRunningJobs();