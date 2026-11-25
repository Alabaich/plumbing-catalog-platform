import { InlineGrid } from '@shopify/polaris';
import { BrandCard } from './BrandCard';
import React from 'react';

interface Brand {
    id: string;
    name: string;
    productCount: number;
}

interface BrandGridProps {
    brands: Brand[];
    selectedBrands: Set<string>;
    onToggleBrand: (id: string, selected: boolean) => void;
}

export function BrandGrid({ brands, selectedBrands, onToggleBrand }: BrandGridProps) {
    return (
        <InlineGrid gap="400" columns={{ xs: 1, sm: 2, md: 3, lg: 4 }}>
            {brands.map((brand) => (
                <BrandCard
                    key={brand.id}
                    id={brand.id}
                    name={brand.name}
                    productCount={brand.productCount}
                    selected={selectedBrands.has(brand.id)}
                    onToggle={onToggleBrand}
                />
            ))}
        </InlineGrid>
    );
}