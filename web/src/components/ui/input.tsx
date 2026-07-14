import * as React from "react";

import { cn } from "../../lib/utils";

const Input = React.forwardRef<HTMLInputElement, React.ComponentProps<"input">>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          "flex h-10 w-full rounded-lg border border-ink-400 bg-ink-100 shadow-[var(--elevation-inset)] px-3.5 py-2 text-sm text-foreground placeholder:text-sm placeholder:text-muted-foreground outline-none transition-[color,background-color,border-color,box-shadow] hover:border-ink-600 focus:border-primary focus:bg-card focus:shadow-none focus:ring-[3px] focus:ring-primary/20 disabled:cursor-not-allowed disabled:opacity-50 file:border-0 file:bg-transparent file:text-sm file:font-medium file:text-foreground",
          type === "password" &&
            "font-mono tracking-wider placeholder:font-sans placeholder:tracking-normal",
          className
        )}
        ref={ref}
        {...props}
      />
    );
  }
);
Input.displayName = "Input";

export { Input };
