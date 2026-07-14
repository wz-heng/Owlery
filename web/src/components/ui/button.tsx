import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";

import { cn } from "../../lib/utils";

/* One button standard. The press gesture is physical and shared by every
 * variant: the button lifts on hover, then travels 1px down and loses its
 * shadow on :active — it is being pressed *into* the paper. That
 * translate-y is what separates a crafted control from a recolored div. */
const buttonVariants = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-lg text-sm font-medium ring-offset-background transition-all duration-150 active:translate-y-px focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0",
  {
    variants: {
      variant: {
        // Filled brass — the one loud element on a page.
        default:
          "bg-primary text-primary-foreground shadow-[var(--elevation-raised)] hover:bg-primary-600 hover:shadow-[var(--elevation-floating)] active:bg-primary-700 active:shadow-none",
        destructive:
          "bg-destructive text-destructive-foreground shadow-[var(--elevation-raised)] hover:bg-destructive/90 hover:shadow-[var(--elevation-floating)] active:bg-destructive/80 active:shadow-none",
        outline:
          "border border-ink-400 bg-card text-foreground shadow-[var(--elevation-raised)] hover:bg-ink-100 hover:border-primary/60 active:bg-ink-200 active:shadow-none",
        secondary:
          "bg-secondary text-secondary-foreground hover:bg-ink-300 active:bg-ink-300/80",
        ghost: "text-primary hover:bg-primary-50 active:bg-primary-100",
        link: "text-primary underline-offset-4 hover:underline active:text-primary-700 active:translate-y-0",
      },
      size: {
        default: "h-10 px-4 py-2",
        sm: "h-9 rounded-lg px-3",
        lg: "h-11 rounded-lg px-8",
        icon: "h-10 w-10",
      },
    },
    defaultVariants: { variant: "default", size: "default" },
  }
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  }
);
Button.displayName = "Button";

export { Button, buttonVariants };
