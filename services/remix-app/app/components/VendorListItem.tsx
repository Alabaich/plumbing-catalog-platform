import {
    Card, InlineStack, Text, Button, ProgressBar,
    BlockStack, ButtonGroup, Box, TextField
} from "@shopify/polaris";
import React, { useEffect, useState } from "react";
import type { ImportJob } from "../jobs.server";

export type ImportStatus = "idle" | "running" | "paused" | "finished" | "error";

interface VendorListItemProps {
    job: ImportJob;
    onStatusChange: (job: ImportJob, status: ImportStatus, progress: number) => void;
    onRemove?: () => void;
    /** While the job is idle, blur on this field persists discount before you start import. */
    onQueueDiscountCommit?: (discountPct: number) => void;
}

export function VendorListItem({ job, onStatusChange, onRemove, onQueueDiscountCommit }: VendorListItemProps) {
    const { vendorId, vendorName, totalCount } = job;
    const status = job.status as ImportStatus;
    const progress = job.progress;
    const pct = totalCount > 0 ? Math.round((progress / totalCount) * 100) : 0;
    const savedDiscount = job.discountPct ?? 0;
    const [discountInput, setDiscountInput] = useState(String(savedDiscount));

    useEffect(() => {
        setDiscountInput(String(job.discountPct ?? 0));
    }, [job.discountPct, job.vendorId]);

    const commitDiscount = () => {
        let n = Number(discountInput);
        if (Number.isNaN(n)) n = 0;
        n = Math.min(100, Math.max(0, n));
        setDiscountInput(String(n));
        if (Math.abs(n - savedDiscount) > 1e-6) {
            onQueueDiscountCommit?.(n);
        }
    };

    const handleStart = () => onStatusChange(job, "running", progress);
    const handlePause = () => onStatusChange(job, "paused", progress);
    const handleResume = () => onStatusChange(job, "running", progress);
    const handleCancel = () => onStatusChange(job, "idle", 0);

    const isFinished = status === "finished";
    const isError = status === "error";
    const isActive = status === "running" || status === "paused" || isError;

    return (
        <Card background={isFinished ? "bg-surface-success" : isError ? "bg-surface-critical" : "bg-surface"}>
            <BlockStack gap="300">
                <InlineStack align="space-between" blockAlign="start">
                    <Box>
                        <Text variant="headingSm" as="h3">{vendorName}</Text>
                        <Text variant="bodySm" as="p" tone="subdued">
                            {isFinished
                                ? "Import complete ✓"
                                : isError
                                    ? "Error — click Resume to retry from last position"
                                    : `${totalCount} total items`}
                            {" · "}
                            Discount {savedDiscount}%
                        </Text>
                        {status === "idle" && onQueueDiscountCommit && (
                            <Box paddingBlockStart="200" maxWidth="140px">
                                <TextField
                                    label="Discount %"
                                    type="number"
                                    suffix="%"
                                    min={0}
                                    max={100}
                                    value={discountInput}
                                    onChange={setDiscountInput}
                                    onBlur={commitDiscount}
                                    autoComplete="off"
                                />
                            </Box>
                        )}
                    </Box>

                    <ButtonGroup>
                        {status === "idle" && (
                            <Button variant="primary" onClick={handleStart}>Start Import</Button>
                        )}
                        {isFinished && (
                            <>
                                <Button tone="success" disabled>Done</Button>
                                {onRemove && (
                                    <Button tone="critical" onClick={onRemove}>Dismiss</Button>
                                )}
                            </>
                        )}
                        {isActive && (
                            <>
                                {status === "running"
                                    ? <Button onClick={handlePause}>Pause</Button>
                                    : <Button variant="primary" onClick={handleResume}>Resume</Button>
                                }
                                <Button tone="critical" onClick={handleCancel}>Cancel</Button>
                            </>
                        )}
                    </ButtonGroup>
                </InlineStack>

                {status !== "idle" && (
                    <BlockStack gap="100">
                        <InlineStack align="space-between">
                            <Text variant="bodySm" as="span" tone={status === "paused" ? "subdued" : "base"}>
                                {pct}% ({progress} / {totalCount})
                                {status === "paused" && " — paused"}
                                {isError && " — failed"}
                            </Text>
                        </InlineStack>
                        <ProgressBar
                            progress={pct}
                            tone={
                                isFinished ? "success"
                                : isError ? "critical"
                                : status === "paused" ? "highlight"
                                : "primary"
                            }
                            size="small"
                            animated={status === "running"}
                        />
                    </BlockStack>
                )}
            </BlockStack>
        </Card>
    );
}