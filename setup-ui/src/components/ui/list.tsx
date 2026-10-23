import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const listVariants = cva("list-none m-0 p-0 flex flex-col", {
  variants: {
    gap: {
      none: "gap-0",
      xs: "gap-1",
      sm: "gap-1.5",
      md: "gap-2",
      lg: "gap-3",
      xl: "gap-4",
    },
  },
  defaultVariants: {
    gap: "sm",
  },
});

const listItemVariants = cva("flex", {
  variants: {
    align: {
      center: "items-center",
      start: "items-start",
      baseline: "items-baseline",
    },
    gap: {
      none: "gap-0",
      xs: "gap-1",
      sm: "gap-1.5",
      md: "gap-2",
      lg: "gap-3",
    },
  },
  defaultVariants: {
    align: "center",
    gap: "sm",
  },
});

type ListElement = "ul" | "ol";

export interface ListProps
  extends React.HTMLAttributes<HTMLUListElement | HTMLOListElement>,
    VariantProps<typeof listVariants> {
  as?: ListElement;
}

export function List({
  as: Tag = "ul",
  gap,
  className,
  ...props
}: ListProps) {
  return <Tag className={cn(listVariants({ gap }), className)} {...props} />;
}

export interface ListItemProps
  extends React.LiHTMLAttributes<HTMLLIElement>,
    VariantProps<typeof listItemVariants> {}

export function ListItem({ align, gap, className, ...props }: ListItemProps) {
  return (
    <li className={cn(listItemVariants({ align, gap }), className)} {...props} />
  );
}
