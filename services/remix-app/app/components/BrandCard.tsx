import { BlockStack, Card, Checkbox, Text } from '@shopify/polaris';
import { useCallback } from 'react';
import React from 'react';

interface BrandCardProps {
    id: string;
    name: string;
    productCount: number;
    selected: boolean;
    onToggle: (id: string, selected: boolean) => void;
}

export function BrandCard({ id, name, productCount, selected, onToggle }: BrandCardProps) {
    const handleChange = useCallback((newChecked: boolean) => {
        onToggle(id, newChecked);
    }, [id, onToggle]);

    return (
        <Card>
            <BlockStack gap="200">
                <Checkbox
                    label={name}
                    checked={selected}
                    onChange={handleChange}
                />
                <Text as="p" tone="subdued">
                    {productCount} products available
                </Text>
            </BlockStack>
        </Card>
    );
}